from __future__ import annotations

from rich.panel import Panel
from rich.text import Text
from textual.widgets import RichLog

from chattergpt.models import Message


class ChatHistory(RichLog):
    def __init__(self) -> None:
        super().__init__(id="chat-history", wrap=True, highlight=False, auto_scroll=True)
        self._messages: list[Message] = []

    def set_messages(self, messages: list[Message]) -> None:
        self._messages = [Message(role=message.role, content=message.content, remote_id=message.remote_id) for message in messages]
        self._render()

    def append_message(self, role: str, content: str) -> None:
        self._messages.append(Message(role=role, content=content))
        self._render()

    def replace_last_assistant(self, content: str) -> None:
        if not self._messages:
            self._messages.append(Message(role="assistant", content=content))
            self._render()
            return
        if self._messages[-1].role == "assistant":
            self._messages[-1] = Message(role="assistant", content=content)
        else:
            self._messages.append(Message(role="assistant", content=content))
        self._render()

    def snapshot_messages(self) -> list[Message]:
        return [Message(role=message.role, content=message.content, remote_id=message.remote_id) for message in self._messages]

    def _render(self) -> None:
        self.clear()
        if not self._messages:
            self.write(Panel(Text("Start a new conversation."), title="Empty Chat", border_style="dim"))
            return
        for message in self._messages:
            self.write(self._panel_for_message(message.role, message.content))

    def _panel_for_message(self, role: str, content: str) -> Panel:
        title = "You" if role == "user" else ("System" if role == "system" else "ChatGPT")
        border = "cyan" if role == "user" else ("yellow" if role == "system" else "green")
        return Panel(Text(content), title=title, border_style=border)
