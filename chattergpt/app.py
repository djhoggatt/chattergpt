from __future__ import annotations

import asyncio
import sys
import termios

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.widgets import Label

from chattergpt.backend.chatgpt_web import ChatGPTWebBackend
from chattergpt.config import load_settings
from chattergpt.models import BackendStatus
from chattergpt.models import AuthState, ConversationSummary, Message, SidebarItem
from chattergpt.store import Store
from chattergpt.widgets.composer import Composer, ComposerSubmitted
from chattergpt.widgets.history import ChatHistory
from chattergpt.widgets.sidebar import Sidebar, SidebarItemSelected


class ChattergptApp(App[None]):
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("alt+up", "sidebar_up", "Sidebar Up", show=False, priority=True),
        Binding("alt+down", "sidebar_down", "Sidebar Down", show=False, priority=True),
        Binding("alt+enter", "sidebar_open", "Open Sidebar Selection", show=False, priority=True),
        Binding("f5", "refresh_backend", "Refresh", show=True),
        Binding("f6", "raise_browser", "Browser", show=True),
    ]

    def __init__(self) -> None:
        super().__init__(ansi_color=True)
        self.settings = load_settings()
        self.store = Store(self.settings.database_path)
        self.backend = ChatGPTWebBackend(self.settings)
        saved_conversation = self.store.get_app_state("last_conversation_remote_id")
        self.current_conversation = (
            ConversationSummary(remote_id=saved_conversation, title="Loading...")
            if saved_conversation
            else ConversationSummary(remote_id=None, title="New Chat", is_new_chat=True)
        )
        self.auth_state = AuthState.UNKNOWN
        self._sending = False
        self._opening_conversation = False
        self._polling_auth = False
        self._refreshing = False
        self._selected_key = self.current_conversation.key
        self._browser_targets = self.backend.list_targets()

    def compose(self) -> ComposeResult:
        with Container(id="sidebar"):
            yield Label("Chats", id="sidebar-title")
            yield Sidebar()
        with Vertical(id="main"):
            yield ChatHistory()
            yield Composer()
            yield Label("Starting backend...", id="status")

    async def on_mount(self) -> None:
        await self._load_cached_sidebar()
        self.query_one(Composer).focus()
        self.set_interval(3.0, self._schedule_auth_poll)
        self.run_worker(self._startup_backend(), exclusive=True, group="startup")

    async def on_unmount(self) -> None:
        self.store.close()
        await self.backend.close()

    async def action_sidebar_up(self) -> None:
        self.query_one(Sidebar).move_selection(-1)

    async def action_sidebar_down(self) -> None:
        self.query_one(Sidebar).move_selection(1)

    async def action_sidebar_open(self) -> None:
        item = self.query_one(Sidebar).current()
        if item is not None:
            await self._activate_sidebar_item(item)

    async def action_refresh_backend(self) -> None:
        self.run_worker(self._refresh_from_backend(), exclusive=True, group="refresh")

    async def action_raise_browser(self) -> None:
        self.run_worker(self._reveal_browser(), exclusive=True, group="browser")

    async def _startup_backend(self) -> None:
        status = self.query_one("#status", Label)
        backend_status = await self.backend.start()
        self.auth_state = backend_status.auth_state
        status.update(self._format_backend_status(backend_status))
        if self.auth_state == AuthState.AUTHENTICATED:
            await self._refresh_from_backend()
        elif self.auth_state == AuthState.LOGIN_REQUIRED:
            history = self.query_one(ChatHistory)
            history.set_messages(
                [Message(role="system", content="Login required. Complete login in the controlled browser window, then press F5.")]
            )
        elif self.auth_state == AuthState.ERROR:
            history = self.query_one(ChatHistory)
            history.set_messages([Message(role="system", content=self._startup_help_text())])

    async def _refresh_from_backend(self) -> None:
        if self._refreshing or self._sending or self._opening_conversation:
            return
        self._refreshing = True
        status = self.query_one("#status", Label)
        try:
            backend_status = await self.backend.check_auth()
            self.auth_state = backend_status.auth_state
            if self.auth_state != AuthState.AUTHENTICATED:
                status.update(self._format_backend_status(backend_status))
                return
            conversations = await self.backend.refresh_conversations()
            self.store.replace_remote_conversations(conversations)
            await self._load_cached_sidebar()
            status.update(f"Synchronized {len(conversations)} conversations from ChatGPT.")
        finally:
            self._refreshing = False

    async def _load_cached_sidebar(self) -> None:
        sidebar = self.query_one(Sidebar)
        sidebar.set_items(self._build_sidebar_items(), selected_key=self._selected_key)

    @on(SidebarItemSelected)
    async def handle_sidebar_selected(self, event: SidebarItemSelected) -> None:
        await self._activate_sidebar_item(event.item)

    async def _activate_sidebar_item(self, item: SidebarItem) -> None:
        self._selected_key = item.key
        if item.kind == "conversation" and item.conversation is not None:
            if self._same_conversation(item.conversation, self.current_conversation):
                return
            await self._open_selected(item.conversation)

    async def _open_selected(self, conversation: ConversationSummary) -> None:
        if self._same_conversation(conversation, self.current_conversation):
            return
        self.current_conversation = conversation
        self._selected_key = conversation.key
        if conversation.remote_id:
            self.store.set_app_state("last_conversation_remote_id", conversation.remote_id)
        history = self.query_one(ChatHistory)
        if conversation.is_new_chat or conversation.remote_id is None:
            self.store.set_app_state("last_conversation_remote_id", "")
            history.set_messages([])
            self.query_one("#status", Label).update("Ready for a new chat.")
            self.query_one(Composer).focus()
            return
        cached = self.store.load_conversation(conversation.remote_id)
        if cached:
            history.set_messages(cached.messages)
        self.query_one("#status", Label).update(f"Loading {conversation.title} from ChatGPT...")
        self.run_worker(self._load_remote_conversation(conversation.remote_id), exclusive=True, group="open")

    async def _load_remote_conversation(self, remote_id: str) -> None:
        history = self.query_one(ChatHistory)
        status = self.query_one("#status", Label)
        self._opening_conversation = True
        try:
            data = await self.backend.open_conversation(remote_id)
        except Exception as exc:
            status.update(f"Failed to open conversation: {exc}")
            return
        finally:
            self._opening_conversation = False
        if self.current_conversation.remote_id != remote_id:
            return
        self.current_conversation = data.summary
        self._selected_key = data.summary.key
        if data.messages:
            self.store.replace_messages(remote_id, data.messages)
            history.set_messages(data.messages)
            status.update(f"Loaded {len(data.messages)} messages.")
        elif history.snapshot_messages():
            status.update("Remote chat opened, but no messages were extracted yet. Keeping current transcript.")
        else:
            history.set_messages([])
            status.update("Opened chat, but no messages were extracted.")
        self.query_one(Composer).focus()

    @on(ComposerSubmitted)
    async def handle_submit(self, event: ComposerSubmitted) -> None:
        if self._sending:
            return
        if self.auth_state != AuthState.AUTHENTICATED:
            self.query_one("#status", Label).update("Login required before sending.")
            return
        self._sending = True
        composer = self.query_one(Composer)
        history = self.query_one(ChatHistory)
        status = self.query_one("#status", Label)
        prompt = event.text
        composer.clear_input()
        history.append_message("user", prompt)
        history.append_message("assistant", "...")
        status.update("Sending message to ChatGPT...")
        self.run_worker(self._send_message(prompt), exclusive=True, group="send")

    async def _send_message(self, prompt: str) -> None:
        history = self.query_one(ChatHistory)
        status = self.query_one("#status", Label)
        remote_id = self.current_conversation.remote_id
        assistant_text = ""
        try:
            events = await self.backend.send_message(remote_id, prompt)
            for event in events:
                if event.kind == "conversation" and event.remote_id:
                    self.current_conversation = ConversationSummary(
                        remote_id=event.remote_id,
                        title=event.title or "Untitled Chat",
                    )
                    self._selected_key = self.current_conversation.key
                    self.store.set_app_state("last_conversation_remote_id", event.remote_id)
                elif event.kind == "assistant_delta":
                    assistant_text += event.text
                    history.replace_last_assistant(assistant_text)
                elif event.kind == "assistant_done":
                    assistant_text = event.text
                    history.replace_last_assistant(assistant_text)
                elif event.kind == "status":
                    status.update(event.text)
            if self.current_conversation.remote_id:
                current_remote_id = self.current_conversation.remote_id
                local_snapshot = history.snapshot_messages()
                self.store.replace_remote_conversations([self.current_conversation])
                self.store.replace_messages(current_remote_id, local_snapshot)
                await self._load_cached_sidebar()
                self.run_worker(
                    self._reconcile_current_conversation(current_remote_id, len(local_snapshot)),
                    exclusive=False,
                    group=f"reconcile-{current_remote_id}",
                )
            status.update("Response captured from ChatGPT.")
        except Exception as exc:
            history.replace_last_assistant(f"[backend error] {exc}")
            status.update(f"Send failed: {exc}")
        finally:
            self._sending = False
            self.query_one(Composer).focus()

    def _schedule_auth_poll(self) -> None:
        if self._polling_auth or self._sending or self._opening_conversation:
            return
        self._polling_auth = True
        self.run_worker(self._poll_auth_state(), exclusive=True, group="auth-poll")

    async def _poll_auth_state(self) -> None:
        try:
            if self._sending or self._opening_conversation:
                return
            backend_status = await self.backend.check_auth()
            previous_state = self.auth_state
            self.auth_state = backend_status.auth_state
            if previous_state != AuthState.AUTHENTICATED and self.auth_state == AuthState.AUTHENTICATED:
                self.query_one("#status", Label).update("Login detected. Synchronizing conversations...")
                await self._refresh_from_backend()
            elif self.auth_state in {AuthState.LOGIN_REQUIRED, AuthState.UNKNOWN} and previous_state != self.auth_state:
                self.query_one("#status", Label).update(self._format_backend_status(backend_status))
        except Exception:
            return
        finally:
            self._polling_auth = False

    async def _reveal_browser(self) -> None:
        status = self.query_one("#status", Label)
        try:
            backend_status = await self.backend.reveal_browser()
        except Exception as exc:
            status.update(f"Could not raise browser window: {exc}")
            return
        status.update(self._format_backend_status(backend_status))

    def _format_backend_status(self, backend_status: BackendStatus) -> str:
        location = backend_status.page_title or backend_status.page_url or "unknown page"
        target = self._settings_target_name()
        return f"{backend_status.detail} [{location}] [browser: {target}]"

    def _settings_target_name(self) -> str:
        return self.settings.selected_browser_name or "unknown"

    def _startup_help_text(self) -> str:
        target = next((target for target in self._browser_targets if target.name == self._settings_target_name()), None)
        if target is None:
            return "No attachable Chromium-family browsers detected."
        if self.settings.auto_launch_browser:
            return (
                f"Chattergpt will try to launch {target.name} automatically with a dedicated profile.\n\n"
                f"Launch command:\n{target.launch_command}"
            )
        return f"Start {target.name} yourself with remote debugging enabled, then press F5.\n\nExample:\n{target.launch_command}"

    async def _reconcile_current_conversation(self, remote_id: str, minimum_messages: int) -> None:
        for _ in range(8):
            if self._sending or self._opening_conversation:
                await asyncio.sleep(0.5)
                continue
            if self.current_conversation.remote_id != remote_id:
                return
            if self.backend.current_remote_id() != remote_id:
                await asyncio.sleep(1.0)
                continue
            try:
                data = await self.backend.open_conversation(remote_id)
            except Exception:
                await asyncio.sleep(1.0)
                continue
            if not data.messages:
                await asyncio.sleep(1.0)
                continue
            self.store.replace_remote_conversations([data.summary])
            self.store.replace_messages(remote_id, data.messages)
            if self.current_conversation.remote_id == remote_id and len(data.messages) >= minimum_messages:
                self.current_conversation = data.summary
                self._selected_key = data.summary.key
                self.query_one(ChatHistory).set_messages(data.messages)
                await self._load_cached_sidebar()
                self.query_one("#status", Label).update(f"Synchronized {len(data.messages)} messages from ChatGPT.")
                return
            await asyncio.sleep(1.0)

    def _same_conversation(self, left: ConversationSummary, right: ConversationSummary) -> bool:
        if left.remote_id is not None or right.remote_id is not None:
            return left.remote_id == right.remote_id
        return left.is_new_chat == right.is_new_chat

    def _build_sidebar_items(self) -> list[SidebarItem]:
        items: list[SidebarItem] = [SidebarItem(kind="section", key="section:chats", label="Chats", selectable=False)]
        for conversation in self.store.list_conversations():
            label = "New Chat" if conversation.is_new_chat else conversation.title
            items.append(
                SidebarItem(
                    kind="conversation",
                    key=conversation.key,
                    label=label,
                    conversation=conversation,
                )
            )
        return items


def main() -> None:
    app = ChattergptApp()
    stdin_attrs = None
    stdin_fd = None
    if sys.stdin.isatty():
        try:
            stdin_fd = sys.stdin.fileno()
            stdin_attrs = termios.tcgetattr(stdin_fd)
        except Exception:
            stdin_attrs = None
            stdin_fd = None
    try:
        app.run()
    finally:
        if stdin_fd is not None and stdin_attrs is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, stdin_attrs)
            except Exception:
                pass
        # Restore common terminal attributes explicitly for terminals that
        # don't fully recover after leaving application mode.
        sys.stdout.write("\x1b[0m\x1b[39m\x1b[49m\x1b[?25h\x1b[0 q")
        sys.stdout.flush()
