from __future__ import annotations

import asyncio
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
import re

from playwright.async_api import Browser, BrowserContext, Error, Page, TimeoutError, async_playwright

from chattergpt.config import BrowserTarget, Settings
from chattergpt.models import (
    AuthState,
    BackendStatus,
    ConversationData,
    ConversationSummary,
    Message,
    ProjectSummary,
    StreamEvent,
)


@dataclass(slots=True)
class Selectors:
    composer_candidates: tuple[str, ...] = (
        '#prompt-textarea.ProseMirror[contenteditable="true"]',
        'div.ProseMirror[contenteditable="true"][aria-label*="ChatGPT"]',
        'div[contenteditable="true"][aria-label*="ChatGPT"]',
        'textarea[placeholder*="Message"]',
        'textarea[placeholder*="Ask"]',
        '[data-testid="composer"] textarea',
        '[data-testid="composer"] div[contenteditable="true"]',
        'form textarea',
    )
    send_button_candidates: tuple[str, ...] = (
        'button[data-testid="send-button"]',
        'button[aria-label^="Send"]',
        'form button[aria-label="Send prompt"]',
        'form button[aria-label="Send message"]',
        'form button.composer-submit-button-color:not([aria-label="Start Voice"])',
        'button[data-testid="send-button"]',
        'button[aria-label^="Send"]',
        'form button[type="submit"]',
    )
    stop_button_candidates: tuple[str, ...] = (
        'button[data-testid="stop-button"]',
        'button[aria-label^="Stop"]',
        'button:has-text("Stop generating")',
        'button:has-text("Stop")',
    )
    sidebar_link_candidates: tuple[str, ...] = (
        'nav a[href*="/c/"]',
        'a[href*="/c/"]',
    )
    project_link_candidates: tuple[str, ...] = (
        'nav a[href*="/project/"]',
        'nav a[href*="/projects/"]',
        'a[href*="/project/"]',
        'a[href*="/projects/"]',
    )
    project_conversation_candidates: tuple[str, ...] = (
        'main a[href*="/g/"][href*="/c/"]',
        'main a[href*="/c/"]',
        'a[href*="/g/"][href*="/c/"]',
    )
    message_candidates: tuple[str, ...] = (
        '[data-message-author-role]',
        'article[data-testid*="conversation"]',
        'main article',
    )
    logged_in_markers: tuple[str, ...] = (
        'textarea[placeholder*="Message"]',
        '[data-testid="composer"]',
        'nav a[href*="/c/"]',
    )
    login_markers: tuple[str, ...] = (
        'a[href*="/auth/login"]',
        'button:has-text("Log in")',
        'button:has-text("Sign up")',
        'input[type="email"]',
    )


class ChatGPTWebBackend:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._selectors = Selectors()
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._target: BrowserTarget | None = settings.browser_target
        self._launched_process: subprocess.Popen | None = None
        self._virtual_display_process: subprocess.Popen | None = None
        self._managed_browser_virtualized = False
        self._managed_browser_virtual_display = ""
        self._launch_note = ""
        self._log_path = settings.backend_log_path

    async def start(self) -> BackendStatus:
        try:
            self._log("backend start requested")
            self._launch_note = ""
            self._playwright = await async_playwright().start()
            target = self._target
            if target is None:
                await self.close()
                return BackendStatus(
                    auth_state=AuthState.ERROR,
                    detail="No supported Chromium-family browser was detected. Set CHATTERGPT_BROWSER to a browser binary.",
                )
            await self._launch_managed_context(target)
            await self._navigate(self._settings.base_url)
            status = await self.check_auth()
            if self._settings.browser_executable_path:
                if self._managed_browser_virtualized:
                    mode_detail = f"Using managed browser in virtual display {self._managed_browser_virtual_display}."
                else:
                    mode_detail = "Using managed visible browser window."
                note = f" {self._launch_note}" if self._launch_note else ""
                status.detail = (
                    f"{status.detail} {mode_detail} Using system browser at {self._settings.browser_executable_path}.{note}"
                )
            return status
        except Exception as exc:
            self._log(f"backend start exception error={exc!r}")
            await self.close()
            return BackendStatus(auth_state=AuthState.ERROR, detail=f"Backend failed to start: {exc}")

    async def close(self) -> None:
        self._log("backend close requested")
        self._context = None
        self._browser = None
        self._launched_process = None
        if self._virtual_display_process is not None and self._virtual_display_process.poll() is None:
            self._virtual_display_process.terminate()
            try:
                self._virtual_display_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._virtual_display_process.kill()
        self._virtual_display_process = None
        self._managed_browser_virtualized = False
        self._managed_browser_virtual_display = ""
        self._launch_note = ""
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._page = None

    async def refresh_conversations(self) -> list[ConversationSummary]:
        page = await self._require_page()
        await self._goto_home()
        for selector in self._selectors.sidebar_link_candidates:
            locator = page.locator(selector)
            try:
                count = await locator.count()
                if count:
                    self._log(f"refresh_conversations matched selector={selector} count={count}")
                    return await self._extract_conversations(locator)
            except Error:
                continue
        self._log(f"refresh_conversations found no sidebar selector {await self._page_summary()}")
        return []

    async def refresh_projects(self) -> list[ProjectSummary]:
        await self._goto_home()
        projects = await self._extract_projects_from_sidebar()
        if projects:
            self._log(f"refresh_projects extracted from sidebar count={len(projects)}")
            return projects
        page = await self._require_page()
        for selector in self._selectors.project_link_candidates:
            locator = page.locator(selector)
            try:
                count = await locator.count()
                if count:
                    self._log(f"refresh_projects matched selector={selector} count={count}")
                    projects = await self._extract_projects(locator)
                    if projects:
                        return projects
            except Error:
                continue
        self._log(f"refresh_projects found no project selector {await self._page_summary()}")
        return []

    async def refresh_project_conversations(self, project: ProjectSummary) -> list[ConversationSummary]:
        page = await self._require_page()
        opened = False
        if project.href:
            await self._navigate(self._full_url(project.href))
            opened = True
            await self._wait_for_project_context(project)
        else:
            opened = await self._open_project_sidebar_entry(project)
        if not opened:
            self._log(f"refresh_project_conversations could not open project={project.remote_id} title={project.title!r}")
            return []
        for selector in self._selectors.project_conversation_candidates:
            locator = page.locator(selector)
            try:
                count = await locator.count()
                if count:
                    self._log(
                        f"refresh_project_conversations matched project selector={selector} "
                        f"project={project.remote_id} count={count}"
                    )
                    conversations = await self._extract_conversations(
                        locator,
                        project_remote_id=project.remote_id,
                    )
                    if conversations:
                        return conversations
            except Error:
                continue
        for selector in self._selectors.sidebar_link_candidates:
            locator = page.locator(selector)
            try:
                count = await locator.count()
                if count:
                    self._log(
                        f"refresh_project_conversations matched selector={selector} "
                        f"project={project.remote_id} count={count}"
                    )
                    return await self._extract_conversations(locator, project_remote_id=project.remote_id)
            except Error:
                continue
        self._log(
            f"refresh_project_conversations found no conversation selector "
            f"project={project.remote_id} {await self._page_summary()}"
        )
        return []

    async def _wait_for_project_context(self, project: ProjectSummary) -> None:
        page = await self._require_page()
        expected_path = urlparse(project.href or "").path if project.href else ""
        for attempt in range(12):
            current_path = urlparse(page.url).path
            if expected_path and current_path == expected_path:
                self._log(
                    f"wait_for_project_context matched url project={project.remote_id} "
                    f"attempt={attempt} path={current_path!r}"
                )
                return
            try:
                nav_text = await page.evaluate(
                    """() => {
                        const nav = document.querySelector('nav');
                        return nav ? nav.innerText : '';
                    }"""
                )
            except Error:
                nav_text = ""
            if project.title and project.title in nav_text:
                self._log(
                    f"wait_for_project_context matched title project={project.remote_id} "
                    f"attempt={attempt}"
                )
                return
            await asyncio.sleep(0.5)
        self._log(f"wait_for_project_context timed out project={project.remote_id} {await self._page_summary()}")

    async def open_conversation(self, remote_id: str | None, href: str | None = None) -> ConversationData:
        page = await self._require_page()
        current_remote_id = self._extract_remote_id(page.url)
        destination = self._conversation_url(remote_id, href)
        if remote_id and destination and page.url != destination:
            await self._navigate(destination)
            await self._wait_for_conversation_messages(remote_id)
        elif remote_id:
            await self._wait_for_conversation_messages(remote_id)
        elif current_remote_id is not None:
            await self._goto_home()
        messages = await self._extract_messages()
        title = await page.title()
        self._log(f"open_conversation remote_id={remote_id} title={title!r} messages={len(messages)}")
        summary = ConversationSummary(
            remote_id=remote_id,
            title=title or "New Chat",
            is_new_chat=remote_id is None,
            href=href,
        )
        return ConversationData(summary=summary, messages=messages)

    async def send_message(self, remote_id: str | None, prompt: str, href: str | None = None) -> list[StreamEvent]:
        page = await self._require_page()
        destination = self._conversation_url(remote_id, href)
        if remote_id and destination:
            await self._navigate(destination)
        else:
            await self._goto_home()
        baseline_messages = await self._extract_messages()
        self._log(f"send_message remote_id={remote_id} baseline_messages={len(baseline_messages)}")
        composer = await self._find_first("composer", self._selectors.composer_candidates)
        if composer is None:
            raise RuntimeError(
                f"Could not find the ChatGPT composer. {await self._page_summary()}. See {self._log_path}."
            )
        await self._enter_prompt(composer, prompt)
        await self._submit_prompt()
        events: list[StreamEvent] = []
        known_remote_id = remote_id
        assistant_text = ""
        conversation_announced = False
        stable_rounds = 0
        for attempt in range(240):
            await asyncio.sleep(self._settings.poll_interval_seconds)
            known_remote_id = known_remote_id or self._extract_remote_id(page.url)
            if known_remote_id and not conversation_announced:
                conversation_announced = True
                page_title = await self._safe_page_title()
                self._log(f"send_message discovered conversation remote_id={known_remote_id} title={page_title!r}")
                events.append(
                    StreamEvent(
                        kind="conversation",
                        remote_id=known_remote_id,
                        title=page_title or "New chat",
                    )
                )
            current_messages = await self._extract_messages()
            latest_assistant = self._latest_new_assistant_text(baseline_messages, current_messages)
            if latest_assistant and latest_assistant != assistant_text:
                assistant_text = latest_assistant
                stable_rounds = 0
                self._log(
                    f"send_message captured assistant candidate remote_id={known_remote_id} assistant_chars={len(assistant_text)}"
                )
            elif assistant_text:
                stable_rounds += 1
            generation_in_progress = await self._is_generation_in_progress(stable_rounds)
            if assistant_text and not generation_in_progress:
                self._log(
                    f"send_message completed remote_id={known_remote_id} assistant_chars={len(assistant_text)}"
                )
                events.append(StreamEvent(kind="assistant_done", text=assistant_text, remote_id=known_remote_id))
                return events
            if attempt % 10 == 0:
                self._log(
                    "send_message polling "
                    f"remote_id={known_remote_id} "
                    f"current_messages={len(current_messages)} "
                    f"assistant_chars={len(assistant_text)} "
                    f"stable_rounds={stable_rounds} "
                    f"generating={generation_in_progress}"
                )
        final_messages = await self._extract_messages()
        final_assistant = self._latest_new_assistant_text(baseline_messages, final_messages)
        if final_assistant:
            assistant_text = final_assistant
        if known_remote_id and not conversation_announced:
            page_title = await self._safe_page_title()
            self._log(f"send_message late conversation remote_id={known_remote_id} title={page_title!r}")
            events.append(
                StreamEvent(
                    kind="conversation",
                    remote_id=known_remote_id,
                    title=page_title or "New chat",
                )
            )
        if assistant_text:
            self._log(f"send_message timeout with partial response remote_id={known_remote_id}")
            events.append(StreamEvent(kind="assistant_done", text=assistant_text, remote_id=known_remote_id))
        else:
            self._log(f"send_message no response detected {await self._page_summary()}")
            events.append(
                StreamEvent(
                    kind="status",
                    text="No assistant response detected before timeout.",
                    remote_id=known_remote_id,
                )
            )
        return events

    async def _is_generation_in_progress(self, stable_rounds: int = 0) -> bool:
        page = await self._require_page()
        for selector in self._selectors.stop_button_candidates:
            locator = page.locator(selector)
            try:
                if await locator.count():
                    self._log(f"generation_in_progress matched stop selector={selector}")
                    return True
            except Error:
                continue
        send_button = await self._find_first("send_button_state", self._selectors.send_button_candidates)
        if send_button is None:
            if stable_rounds >= 3:
                self._log("generation_in_progress treating missing send button as complete after stable rounds")
                return False
            return True
        try:
            disabled = bool(await send_button.evaluate("(node) => !!node.disabled"))
        except Error:
            disabled = False
        return disabled

    def _latest_new_assistant_text(self, baseline_messages: list[Message], current_messages: list[Message]) -> str:
        baseline_count = len(baseline_messages)
        if len(current_messages) > baseline_count:
            candidates = current_messages[baseline_count:]
        else:
            candidates = current_messages
        for message in reversed(candidates):
            if message.role == "assistant" and message.content.strip():
                return message.content.strip()
        return ""

    async def check_auth(self) -> BackendStatus:
        page = await self._require_page()
        auth_state, detail = await self._detect_auth_state()
        page_title = None
        try:
            page_title = await page.title()
        except Error:
            page_title = None
        return BackendStatus(
            auth_state=auth_state,
            detail=detail,
            page_url=page.url,
            page_title=page_title,
        )

    async def reveal_browser(self) -> BackendStatus:
        if self._managed_browser_virtualized:
            return BackendStatus(
                auth_state=AuthState.AUTHENTICATED,
                detail=(
                    "Managed browser is running inside a virtual display. "
                    "Restart with CHATTERGPT_DISPLAY_BROWSER=1 to inspect it."
                ),
                page_url=self._page.url if self._page is not None else None,
                page_title=await self._safe_page_title() if self._page is not None else None,
            )
        page = await self._require_page()
        await page.bring_to_front()
        return await self.check_auth()

    def current_remote_id(self) -> str | None:
        if self._page is None:
            return None
        return self._extract_remote_id(self._page.url)

    async def _goto_home(self) -> None:
        await self._navigate(self._settings.base_url)

    async def _launch_managed_context(self, target: BrowserTarget) -> None:
        launch_env = dict(os.environ)
        if self._settings.virtual_display_executable and not self._settings.display_browser:
            display = self._start_virtual_display()
            if display is not None:
                launch_env["DISPLAY"] = display
                self._managed_browser_virtualized = True
                self._managed_browser_virtual_display = display
            else:
                self._managed_browser_virtualized = False
                self._managed_browser_virtual_display = ""
                if not self._launch_note:
                    self._launch_note = "Virtual display was requested but unavailable, so the browser is visible."
        self._browser = await self._connect_or_launch_target(target, env=launch_env)
        self._context = self._select_context(self._browser)
        self._page = await self._select_page()
        self._log(
            "launch mode connected to managed browser "
            f"target={target.name} "
            f"virtualized={self._managed_browser_virtualized} "
            f"display={self._managed_browser_virtual_display or '(visible)'}"
        )

    def _start_virtual_display(self) -> str | None:
        if self._virtual_display_process is not None and self._virtual_display_process.poll() is None:
            return self._managed_browser_virtual_display or None
        executable = self._settings.virtual_display_executable
        if not executable:
            self._log("virtual display requested but no virtual display executable was detected")
            self._launch_note = "No virtual display executable was detected."
            return None
        display_number = self._find_available_display_number(self._settings.virtual_display_number)
        display = f":{display_number}"
        command = [
            executable,
            display,
            "-screen",
            "0",
            self._settings.virtual_display_size,
            "-nolisten",
            "tcp",
        ]
        self._virtual_display_process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(20):
            if self._virtual_display_process.poll() is not None:
                self._log(f"virtual display exited immediately display={display}")
                self._launch_note = "Virtual display failed to start."
                self._virtual_display_process = None
                self._managed_browser_virtual_display = ""
                return None
            if Path(f"/tmp/.X11-unix/X{display_number}").exists():
                break
            time.sleep(0.1)
        else:
            self._log(f"virtual display socket did not appear display={display}")
            self._launch_note = "Virtual display did not become ready."
            return None
        self._managed_browser_virtual_display = display
        self._log(f"started virtual display display={display} command={command!r}")
        return display

    def _find_available_display_number(self, start: int) -> int:
        display = start
        while Path(f"/tmp/.X11-unix/X{display}").exists():
            display += 1
        return display

    async def _navigate(self, url: str) -> None:
        page = await self._require_page()
        self._log(f"navigate url={url}")
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=2_000)
        except TimeoutError:
            # ChatGPT often keeps background network activity alive indefinitely.
            pass
        self._log(f"navigate complete {await self._page_summary()}")

    async def _wait_for_conversation_messages(self, remote_id: str) -> None:
        page = await self._require_page()
        for attempt in range(12):
            current_remote_id = self._extract_remote_id(page.url)
            if current_remote_id == remote_id:
                try:
                    count = await page.locator('[data-message-author-role]').count()
                except Error:
                    count = 0
                if count:
                    self._log(
                        f"wait_for_conversation_messages remote_id={remote_id} attempt={attempt} count={count}"
                    )
                    return
            await asyncio.sleep(0.5)
        self._log(f"wait_for_conversation_messages timed out remote_id={remote_id} {await self._page_summary()}")

    async def _require_page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Playwright page is not ready.")
        return self._page

    async def _enter_prompt(self, composer, prompt: str) -> None:
        page = await self._require_page()
        tag_name = ""
        try:
            tag_name = str(await composer.evaluate("(node) => node.tagName.toLowerCase()"))
        except Error:
            tag_name = ""
        await composer.click()
        self._log(f"enter_prompt tag={tag_name!r} chars={len(prompt)}")
        if tag_name == "textarea":
            try:
                await composer.fill(prompt)
                self._log("enter_prompt used textarea.fill")
                return
            except Error as exc:
                self._log(f"enter_prompt textarea.fill failed error={exc!r}")
        try:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
        except Error:
            pass
        lines = prompt.split("\n")
        for index, line in enumerate(lines):
            if line:
                await page.keyboard.type(line)
            if index < len(lines) - 1:
                await page.keyboard.press("Shift+Enter")
        self._log("enter_prompt used keyboard typing into contenteditable")

    async def _submit_prompt(self) -> None:
        page = await self._require_page()
        send_button = await self._find_first("send_button", self._selectors.send_button_candidates)
        if send_button is not None:
            try:
                disabled = bool(await send_button.evaluate("(node) => !!node.disabled"))
            except Error:
                disabled = False
            if not disabled:
                await send_button.click()
                self._log("submit_prompt clicked send button")
                return
        await page.keyboard.press("Enter")
        self._log("submit_prompt used Enter key")

    async def _connect_or_launch_target(self, target: BrowserTarget, *, env: dict[str, str] | None = None) -> Browser:
        first_error: Exception | None = None
        try:
            self._log(f"connect_over_cdp initial target={target.name} url={target.cdp_url}")
            return await self._playwright.chromium.connect_over_cdp(target.cdp_url)
        except Exception as exc:
            first_error = exc
            self._log(f"connect_over_cdp initial failed target={target.name} error={exc!r}; launching")
        self._launch_target(target, env=env)
        for _ in range(30):
            try:
                self._log(f"connect_over_cdp retry target={target.name}")
                return await self._playwright.chromium.connect_over_cdp(target.cdp_url)
            except Exception as exc:
                first_error = first_error or exc
                await asyncio.sleep(0.5)
        if first_error is not None:
            raise first_error
        raise RuntimeError(f"Unable to connect to launched browser target {target.name}.")

    def _launch_target(self, target: BrowserTarget, env: dict[str, str] | None = None) -> None:
        if self._launched_process is not None and self._launched_process.poll() is None:
            return
        command = [
            target.executable_path,
            *target.launch_args,
            f"--user-data-dir={target.profile_dir}",
            self._settings.base_url,
        ]
        launch_env = env or os.environ.copy()
        self._launched_process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=launch_env,
        )
        self._log(
            "launched browser "
            f"target={target.name} "
            f"pid={self._launched_process.pid} "
            f"display={launch_env.get('DISPLAY', '(default)')} "
            f"command={command!r}"
        )

    def _select_context(self, browser: Browser) -> BrowserContext:
        if browser.contexts:
            return browser.contexts[0]
        raise RuntimeError(
            "Connected browser has no contexts. Start the browser normally with remote debugging enabled."
        )

    async def _select_page(self) -> Page:
        if self._context is None:
            raise RuntimeError("Browser context is not ready.")
        for page in self._context.pages:
            if "chatgpt.com" in page.url:
                self._log(f"selected existing chatgpt page url={page.url}")
                return page
        self._log("no existing chatgpt page found; opening new page")
        return await self._context.new_page()

    async def _detect_auth_state(self) -> tuple[AuthState, str]:
        page = await self._require_page()
        challenge_markers = (
            'text="Verify you are human"',
            'text="Just a moment..."',
            'text="Checking your browser"',
            'iframe[title*="challenge"]',
            '[data-translate="challenge"]',
        )
        page_title = await self._safe_page_title()
        if page_title in {"Just a moment...", "Attention Required! | Cloudflare"}:
            self._log(f"auth_state challenge by title title={page_title!r}")
            if self._managed_browser_virtualized:
                return (
                    AuthState.UNKNOWN,
                    "Challenge page detected inside the virtual display. Restart with CHATTERGPT_DISPLAY_BROWSER=1, complete the check, then run normally again.",
                )
            return (
                AuthState.UNKNOWN,
                "Challenge page detected. Complete the verification in the controlled browser window.",
            )
        for selector in challenge_markers:
            try:
                if await page.locator(selector).count():
                    self._log(f"auth_state challenge marker matched selector={selector}")
                    if self._managed_browser_virtualized:
                        return (
                            AuthState.UNKNOWN,
                            "Challenge page detected inside the virtual display. Restart with CHATTERGPT_DISPLAY_BROWSER=1, complete the check, then run normally again.",
                        )
                    return (
                        AuthState.UNKNOWN,
                        f"Challenge page detected. Complete the verification in the browser window. Matched selector: {selector}",
                    )
            except Error:
                continue
        for selector in self._selectors.logged_in_markers:
            try:
                if await page.locator(selector).count():
                    self._log(f"auth_state authenticated selector={selector}")
                    return (
                        AuthState.AUTHENTICATED,
                        f"Authenticated session loaded. Matched selector: {selector}",
                    )
            except Error:
                continue
        for selector in self._selectors.login_markers:
            try:
                if await page.locator(selector).count():
                    self._log(f"auth_state login_required selector={selector}")
                    return (
                        AuthState.LOGIN_REQUIRED,
                        f"Login required in the controlled browser window. Matched selector: {selector}",
                    )
            except Error:
                continue
        if "auth" in page.url:
            self._log("auth_state login_required by url")
            return (
                AuthState.LOGIN_REQUIRED,
                "Login required in the controlled browser window. Page URL indicates auth flow.",
            )
        self._log(f"auth_state unknown {await self._page_summary()}")
        return (
            AuthState.UNKNOWN,
            "Unable to determine ChatGPT page state from known selectors. Press F6 to inspect the browser window.",
        )

    async def _find_first(self, label: str, selectors: tuple[str, ...]):
        page = await self._require_page()
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = await locator.count()
                if count:
                    self._log(f"find_first label={label} selector={selector} count={count}")
                    return locator.first
            except Error:
                continue
        self._log(f"find_first label={label} no matches {await self._page_summary()}")
        return None

    async def _extract_conversations(self, locator, project_remote_id: str | None = None) -> list[ConversationSummary]:
        count = min(await locator.count(), self._settings.sync_limit)
        items: list[ConversationSummary] = []
        seen_ids: set[str] = set()
        for index in range(count):
            entry = locator.nth(index)
            href = await entry.get_attribute("href")
            if not href:
                continue
            if "/c/" not in href:
                continue
            remote_id = self._extract_remote_id(self._full_url(href))
            if remote_id is None or remote_id in seen_ids:
                continue
            title = (await entry.inner_text()).strip() or "Untitled Chat"
            items.append(
                ConversationSummary(
                    remote_id=remote_id,
                    title=title,
                    project_remote_id=project_remote_id,
                    href=self._full_url(href),
                )
            )
            seen_ids.add(remote_id)
        self._log(f"extracted conversations count={len(items)}")
        return items

    async def _extract_projects(self, locator) -> list[ProjectSummary]:
        count = await locator.count()
        items: list[ProjectSummary] = []
        seen_ids: set[str] = set()
        for index in range(count):
            entry = locator.nth(index)
            href = await entry.get_attribute("href")
            if not href:
                continue
            remote_id = self._extract_project_remote_id(href)
            if remote_id is None or remote_id in seen_ids:
                continue
            title = (await entry.inner_text()).strip() or "Untitled Project"
            items.append(ProjectSummary(remote_id=remote_id, title=title, href=href))
            seen_ids.add(remote_id)
        self._log(f"extracted projects count={len(items)}")
        return items

    async def _extract_projects_from_sidebar(self) -> list[ProjectSummary]:
        page = await self._require_page()
        try:
            raw_items = await page.evaluate(
                """() => {
                    const nav = document.querySelector('nav');
                    if (!nav) return [];
                    const interactive = [...nav.querySelectorAll('a[href], button, [role="button"]')];
                    return interactive.map((node, index) => ({
                        index,
                        href: node.getAttribute('href') || '',
                        text: (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim(),
                        aria: node.getAttribute('aria-label') || '',
                    })).filter(item => item.text);
                }"""
            )
        except Error:
            return []
        if not raw_items:
            return []
        section_headings = {
            "new chat",
            "chats",
            "gpts",
            "explore gpts",
            "library",
            "recents",
            "recent",
            "today",
            "yesterday",
            "settings",
            "upgrade",
        }
        projects: list[ProjectSummary] = []
        seen_ids: set[str] = set()
        in_projects = False
        for item in raw_items:
            text = str(item.get("text") or "").strip()
            href = str(item.get("href") or "").strip() or None
            lower = text.lower()
            if lower == "projects":
                in_projects = True
                continue
            if not in_projects:
                if href and self._extract_project_remote_id(href):
                    remote_id = self._extract_project_remote_id(href)
                    if remote_id and remote_id not in seen_ids:
                        projects.append(ProjectSummary(remote_id=remote_id, title=text or "Untitled Project", href=href))
                        seen_ids.add(remote_id)
                continue
            if lower in section_headings:
                break
            if href and "/c/" in href:
                continue
            if lower in {"new project", "see more", "show more"}:
                continue
            if not text:
                continue
            remote_id = self._extract_project_remote_id(href) if href else None
            if remote_id is None:
                remote_id = self._slugify_project_title(text)
            if remote_id in seen_ids:
                continue
            projects.append(ProjectSummary(remote_id=remote_id, title=text, href=href))
            seen_ids.add(remote_id)
        return projects

    async def _extract_messages(self) -> list[Message]:
        page = await self._require_page()
        for selector in self._selectors.message_candidates:
            locator = page.locator(selector)
            try:
                count = await locator.count()
            except Error:
                continue
            if not count:
                continue
            messages: list[Message] = []
            for index in range(count):
                node = locator.nth(index)
                try:
                    text = (await node.inner_text()).strip()
                except Error:
                    continue
                if not text:
                    continue
                role = await node.get_attribute("data-message-author-role")
                remote_id = await node.get_attribute("data-message-id")
                inferred = self._infer_role(role, text)
                messages.append(Message(role=inferred, content=text, remote_id=remote_id))
            if messages:
                self._log(f"extract_messages selector={selector} count={len(messages)}")
                return messages
        self._log(f"extract_messages found no messages {await self._page_summary()}")
        return []

    def _infer_role(self, role: str | None, text: str) -> str:
        if role in {"assistant", "user", "system"}:
            return role
        if text.startswith("You said:"):
            return "user"
        return "assistant"

    def _extract_remote_id(self, url: str) -> str | None:
        path = urlparse(url).path.strip("/")
        if not path:
            return None
        pieces = path.split("/")
        if len(pieces) >= 2 and pieces[0] == "c":
            return pieces[1]
        if "c" in pieces:
            index = pieces.index("c")
            if len(pieces) > index + 1:
                return pieces[index + 1]
        return None

    def _conversation_url(self, remote_id: str | None, href: str | None) -> str | None:
        if href:
            return self._full_url(href)
        if remote_id:
            return f"{self._settings.base_url}c/{remote_id}"
        return None

    def _extract_project_remote_id(self, href: str) -> str | None:
        path = urlparse(href).path.strip("/")
        if not path:
            return None
        pieces = path.split("/")
        if len(pieces) >= 2 and pieces[0] in {"project", "projects"}:
            return pieces[1]
        return None

    def _slugify_project_title(self, title: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        return f"title:{slug or 'project'}"

    async def _open_project_sidebar_entry(self, project: ProjectSummary) -> bool:
        page = await self._require_page()
        candidates = []
        if project.href:
            candidates.append(f'nav a[href="{project.href}"]')
        title = project.title.replace("\\", "\\\\").replace('"', '\\"')
        candidates.extend(
            [
                f'nav a:has-text("{title}")',
                f'nav button:has-text("{title}")',
                f'nav [role="button"]:has-text("{title}")',
            ]
        )
        for selector in candidates:
            locator = page.locator(selector)
            try:
                if await locator.count():
                    await locator.first.click()
                    self._log(f"open_project_sidebar_entry clicked selector={selector} title={project.title!r}")
                    await page.wait_for_timeout(750)
                    return True
            except Error:
                continue
        return False

    def _full_url(self, href: str) -> str:
        if href.startswith("http://") or href.startswith("https://"):
            return href
        if href.startswith("/"):
            return f"{self._settings.base_url.rstrip('/')}{href}"
        return f"{self._settings.base_url}{href}"

    async def _page_summary(self) -> str:
        page = await self._require_page()
        title = await self._safe_page_title()
        return f"url={page.url!r} title={title!r}"

    async def _safe_page_title(self) -> str:
        page = await self._require_page()
        try:
            return await page.title()
        except Error:
            return ""

    def _log(self, message: str) -> None:
        if self._log_path is None:
            return
        try:
            log_path = Path(self._log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{self._settings.log_timestamp()} {message}\n")
        except Exception:
            pass
