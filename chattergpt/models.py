from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class AuthState(str, Enum):
    UNKNOWN = "unknown"
    AUTHENTICATED = "authenticated"
    LOGIN_REQUIRED = "login_required"
    ERROR = "error"


Role = Literal["user", "assistant", "system"]


@dataclass(slots=True)
class ConversationSummary:
    remote_id: str | None
    title: str
    updated_at: str | None = None
    is_new_chat: bool = False
    project_remote_id: str | None = None
    href: str | None = None

    @property
    def key(self) -> str:
        return self.remote_id or "__new_chat__"


@dataclass(slots=True)
class ProjectSummary:
    remote_id: str
    title: str
    href: str | None = None
    updated_at: str | None = None

    @property
    def key(self) -> str:
        return f"project:{self.remote_id}"


@dataclass(slots=True)
class Message:
    role: Role
    content: str
    remote_id: str | None = None


@dataclass(slots=True)
class ConversationData:
    summary: ConversationSummary
    messages: list[Message] = field(default_factory=list)


@dataclass(slots=True)
class BackendStatus:
    auth_state: AuthState
    detail: str
    page_url: str | None = None
    page_title: str | None = None


@dataclass(slots=True)
class StreamEvent:
    kind: Literal["conversation", "assistant_delta", "assistant_done", "status"]
    text: str = ""
    remote_id: str | None = None
    title: str | None = None


@dataclass(slots=True)
class SidebarItem:
    kind: Literal["section", "conversation", "project", "back"]
    key: str
    label: str
    selectable: bool = True
    conversation: ConversationSummary | None = None
    project: ProjectSummary | None = None
