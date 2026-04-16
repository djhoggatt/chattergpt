from __future__ import annotations

from textual import events
from textual.message import Message as TextualMessage
from textual.widgets import Label, ListItem, ListView

from chattergpt.models import SidebarItem


class SidebarItemSelected(TextualMessage):
    def __init__(self, item: SidebarItem) -> None:
        self.item = item
        super().__init__()


class Sidebar(ListView):
    def __init__(self) -> None:
        super().__init__(id="sidebar-list")
        self.items: list[SidebarItem] = []
        self._list_items: list[ListItem] = []
        self._suppress_selection_events = False

    def set_items(self, items: list[SidebarItem], selected_key: str | None = None) -> None:
        self._suppress_selection_events = True
        self.clear()
        self.items = items
        self._list_items = []
        selected_index = self._first_selectable_index()
        for item in items:
            item_label = Label(item.label)
            if not item.selectable:
                item_label.add_class("sidebar-section")
            list_item = ListItem(item_label, name=item.key, disabled=not item.selectable)
            self._list_items.append(list_item)
            self.append(list_item)
        if selected_key is not None:
            matched_index = self._index_for_key(selected_key)
            if matched_index is not None:
                selected_index = matched_index
        if items and selected_index is not None:
            self.index = selected_index
            self._apply_selection_style()
        self._suppress_selection_events = False

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._apply_selection_style()
        if self._suppress_selection_events:
            return
        item = self._item_from_index(event.list_view.index)
        if item is not None and item.selectable:
            self.post_message(SidebarItemSelected(item))

    async def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            item = self._item_from_index(self.index)
            if item is not None and item.selectable:
                self.post_message(SidebarItemSelected(item))
                event.stop()

    def move_selection(self, delta: int) -> SidebarItem | None:
        if not self.items:
            return None
        if self.index is None:
            self.index = self._first_selectable_index()
        next_index = self.index if self.index is not None else 0
        while True:
            next_index = max(0, min(len(self.items) - 1, next_index + delta))
            if self.items[next_index].selectable:
                self.index = next_index
                self._apply_selection_style()
                return self._item_from_index(self.index)
            if next_index in {0, len(self.items) - 1}:
                return self._item_from_index(self.index)

    def current(self) -> SidebarItem | None:
        return self._item_from_index(self.index)

    def _item_from_index(self, index: int | None) -> SidebarItem | None:
        if index is None or index < 0 or index >= len(self.items):
            return None
        return self.items[index]

    def _first_selectable_index(self) -> int | None:
        for index, item in enumerate(self.items):
            if item.selectable:
                return index
        return None

    def _index_for_key(self, key: str) -> int | None:
        for index, item in enumerate(self.items):
            if item.key == key and item.selectable:
                return index
        return None

    def _apply_selection_style(self) -> None:
        for index, list_item in enumerate(self._list_items):
            if self.items[index].selectable and index == self.index:
                list_item.add_class("sidebar-selected")
            else:
                list_item.remove_class("sidebar-selected")
