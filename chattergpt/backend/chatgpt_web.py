from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Error, Page, TimeoutError, async_playwright

from chattergpt.config import BrowserTarget, Settings
from chattergpt.models import AuthState, BackendStatus, ConversationData, ConversationSummary, Message, StreamEvent


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
    sidebar_link_candidates: tuple[str, ...] = (
        'nav a[href*="/c/"]',
        'a[href*="/c/"]',
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
        self._attached = False
        self._target: BrowserTarget | None = self._selected_target()
        self._launched_process: subprocess.Popen | None = None
        self._log_path = settings.backend_log_path

    async def start(self) -> BackendStatus:
        try:
            self._log("backend start requested")
            self._playwright = await async_playwright().start()
            if self._settings.backend_mode == "attach":
                target = self._selected_target()
                if target is None:
                    await self.close()
                    return BackendStatus(
                        auth_state=AuthState.ERROR,
                        detail="No supported Chromium-family browsers were detected for attach mode.",
                    )
                self._target = target
                self._attached = True
                self._log(f"attach mode using target={target.name} cdp_url={target.cdp_url}")
                try:
                    self._browser = await self._connect_or_launch_target(target)
                except Exception as exc:
                    self._log(f"attach failed target={target.name} error={exc!r}")
                    await self.close()
                    return BackendStatus(
                        auth_state=AuthState.ERROR,
                        detail=(
                            f"Could not attach to {target.name} at {target.cdp_url}: {exc}\n"
                            f"Launch command:\n{target.launch_command}"
                        ),
                    )
                self._context = self._select_context(self._browser)
                self._page = await self._select_page()
            else:
                launch_options = {
                    "user_data_dir": str(self._settings.browser_profile_dir),
                    "headless": self._settings.headless,
                    "viewport": {"width": 1440, "height": 1200},
                }
                if self._settings.browser_executable_path:
                    launch_options["executable_path"] = self._settings.browser_executable_path
                self._context = await self._playwright.chromium.launch_persistent_context(**launch_options)
                self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
                self._log("launch mode started persistent context")
            await self._navigate(self._settings.base_url)
            status = await self.check_auth()
            if self._attached and self._target is not None:
                status.detail = (
                    f"{status.detail} Attached to {self._target.name} at {self._target.cdp_url}."
                )
            elif self._settings.browser_executable_path:
                status.detail = (
                    f"{status.detail} Using system browser at {self._settings.browser_executable_path}."
                )
            return status
        except Exception as exc:
            self._log(f"backend start exception error={exc!r}")
            await self.close()
            return BackendStatus(auth_state=AuthState.ERROR, detail=f"Backend failed to start: {exc}")

    async def close(self) -> None:
        self._log("backend close requested")
        if self._context is not None and not self._attached:
            await self._context.close()
        self._context = None
        self._browser = None
        self._launched_process = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._page = None
        self._attached = False

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

    async def open_conversation(self, remote_id: str | None) -> ConversationData:
        page = await self._require_page()
        current_remote_id = self._extract_remote_id(page.url)
        if remote_id and current_remote_id != remote_id:
            await self._navigate(f"{self._settings.base_url}c/{remote_id}")
            await self._wait_for_conversation_messages(remote_id)
        elif remote_id:
            await self._wait_for_conversation_messages(remote_id)
        elif current_remote_id is not None:
            await self._goto_home()
        messages = await self._extract_messages()
        title = await page.title()
        self._log(f"open_conversation remote_id={remote_id} title={title!r} messages={len(messages)}")
        summary = ConversationSummary(remote_id=remote_id, title=title or "New Chat", is_new_chat=remote_id is None)
        return ConversationData(summary=summary, messages=messages)

    async def send_message(self, remote_id: str | None, prompt: str) -> list[StreamEvent]:
        page = await self._require_page()
        if remote_id:
            await self._navigate(f"{self._settings.base_url}c/{remote_id}")
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
        for _ in range(160):
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
            if len(current_messages) > len(baseline_messages):
                latest = current_messages[-1]
                if latest.role == "assistant":
                    delta = latest.content[len(assistant_text) :]
                    if delta:
                        assistant_text = latest.content
                        stable_rounds = 0
                        events.append(StreamEvent(kind="assistant_delta", text=delta, remote_id=known_remote_id))
                    else:
                        stable_rounds += 1
            elif current_messages:
                latest = current_messages[-1]
                if latest.role == "assistant":
                    delta = latest.content[len(assistant_text) :]
                    if delta:
                        assistant_text = latest.content
                        stable_rounds = 0
                        events.append(StreamEvent(kind="assistant_delta", text=delta, remote_id=known_remote_id))
                    elif assistant_text:
                        stable_rounds += 1
            if assistant_text and stable_rounds >= 3:
                self._log(
                    f"send_message completed remote_id={known_remote_id} assistant_chars={len(assistant_text)}"
                )
                events.append(StreamEvent(kind="assistant_done", text=assistant_text, remote_id=known_remote_id))
                return events
            if _ % 10 == 0:
                self._log(
                    f"send_message polling remote_id={known_remote_id} current_messages={len(current_messages)} assistant_chars={len(assistant_text)}"
                )
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
        page = await self._require_page()
        await page.bring_to_front()
        return await self.check_auth()

    def set_target(self, target_name: str) -> None:
        self._settings.selected_browser_name = target_name
        self._target = self._selected_target()

    def list_targets(self) -> list[BrowserTarget]:
        return list(self._settings.browser_targets or [])

    async def _goto_home(self) -> None:
        await self._navigate(self._settings.base_url)

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

    async def _connect_or_launch_target(self, target: BrowserTarget) -> Browser:
        try:
            self._log(f"connect_over_cdp initial target={target.name} url={target.cdp_url}")
            return await self._playwright.chromium.connect_over_cdp(target.cdp_url)
        except Exception as first_error:
            if not self._settings.auto_launch_browser:
                raise first_error
            self._log(f"connect_over_cdp initial failed target={target.name} error={first_error!r}; launching")
            self._launch_target(target)
            for _ in range(30):
                try:
                    self._log(f"connect_over_cdp retry target={target.name}")
                    return await self._playwright.chromium.connect_over_cdp(target.cdp_url)
                except Exception:
                    await asyncio.sleep(0.5)
            raise first_error

    def _launch_target(self, target: BrowserTarget) -> None:
        if self._launched_process is not None and self._launched_process.poll() is None:
            return
        command = [
            target.executable_path,
            *target.launch_args,
            f"--user-data-dir={target.profile_dir}",
            self._settings.base_url,
        ]
        self._launched_process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._log(f"launched browser target={target.name} pid={self._launched_process.pid} command={command!r}")

    def _selected_target(self) -> BrowserTarget | None:
        targets = self._settings.browser_targets or []
        if self._settings.selected_browser_name:
            for target in targets:
                if target.name == self._settings.selected_browser_name:
                    return target
        return targets[0] if targets else None

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
            'iframe[title*="challenge"]',
            '[data-translate="challenge"]',
        )
        for selector in challenge_markers:
            try:
                if await page.locator(selector).count():
                    self._log(f"auth_state challenge marker matched selector={selector}")
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

    async def _extract_conversations(self, locator) -> list[ConversationSummary]:
        count = min(await locator.count(), self._settings.sync_limit)
        items: list[ConversationSummary] = []
        for index in range(count):
            entry = locator.nth(index)
            href = await entry.get_attribute("href")
            if not href:
                continue
            remote_id = href.rstrip("/").split("/")[-1]
            title = (await entry.inner_text()).strip() or "Untitled Chat"
            items.append(ConversationSummary(remote_id=remote_id, title=title))
        self._log(f"extracted conversations count={len(items)}")
        return items

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
        return None

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
