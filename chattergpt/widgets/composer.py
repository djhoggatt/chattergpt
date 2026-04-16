from __future__ import annotations

from textual import events
from textual.binding import Binding
from textual.message import Message as TextualMessage
from textual.widgets import TextArea


class ComposerSubmitted(TextualMessage):
    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()


class Composer(TextArea):
    BINDINGS = [
        Binding("shift+enter", "insert_newline", "New Line", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(id="composer")
        self.border_title = "Message"
        self.show_line_numbers = False

    async def submit(self) -> None:
        text = self.text.rstrip()
        if not text:
            return
        self.post_message(ComposerSubmitted(text))

    async def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            await self.submit()

    def action_insert_newline(self) -> None:
        self.insert("\n")

    def clear_input(self) -> None:
        self.text = ""
