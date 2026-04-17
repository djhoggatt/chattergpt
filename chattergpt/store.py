from __future__ import annotations

import sqlite3
from pathlib import Path

from chattergpt.models import ConversationData, ConversationSummary, Message, ProjectSummary


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    remote_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    updated_at TEXT,
    project_remote_id TEXT,
    href TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS projects (
    remote_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    href TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS local_conversations (
    local_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    updated_at TEXT,
    is_new_chat INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_remote_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    remote_message_id TEXT,
    UNIQUE(conversation_remote_id, position)
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._ensure_schema()
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def replace_remote_conversations(self, conversations: list[ConversationSummary]) -> None:
        with self._connection:
            for sort_order, conversation in enumerate(conversations):
                if not conversation.remote_id:
                    continue
                self._connection.execute(
                    """
                    INSERT INTO conversations (remote_id, title, updated_at, project_remote_id, href, sort_order, last_synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(remote_id) DO UPDATE SET
                        title = excluded.title,
                        updated_at = excluded.updated_at,
                        project_remote_id = excluded.project_remote_id,
                        href = excluded.href,
                        sort_order = excluded.sort_order,
                        last_synced_at = CURRENT_TIMESTAMP
                    """,
                    (
                        conversation.remote_id,
                        conversation.title,
                        conversation.updated_at,
                        conversation.project_remote_id,
                        conversation.href,
                        sort_order,
                    ),
                )

    def replace_projects(self, projects: list[ProjectSummary]) -> None:
        with self._connection:
            for sort_order, project in enumerate(projects):
                self._connection.execute(
                    """
                    INSERT INTO projects (remote_id, title, href, sort_order, last_synced_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(remote_id) DO UPDATE SET
                        title = excluded.title,
                        href = excluded.href,
                        sort_order = excluded.sort_order,
                        last_synced_at = CURRENT_TIMESTAMP
                    """,
                    (project.remote_id, project.title, project.href, sort_order),
                )

    def list_conversations(self, project_remote_id: str | None = None) -> list[ConversationSummary]:
        if project_remote_id is None:
            rows = self._connection.execute(
                """
                SELECT remote_id, title, updated_at, project_remote_id, href
                FROM conversations
                WHERE project_remote_id IS NULL
                ORDER BY sort_order ASC, COALESCE(updated_at, '') DESC, rowid DESC
                """
            ).fetchall()
        else:
            rows = self._connection.execute(
                """
                SELECT remote_id, title, updated_at, project_remote_id, href
                FROM conversations
                WHERE project_remote_id = ?
                ORDER BY sort_order ASC, COALESCE(updated_at, '') DESC, rowid DESC
                """,
                (project_remote_id,),
            ).fetchall()
        items = [
            ConversationSummary(
                remote_id=row["remote_id"],
                title=row["title"],
                updated_at=row["updated_at"],
                project_remote_id=row["project_remote_id"],
                href=row["href"],
            )
            for row in rows
        ]
        if project_remote_id is None:
            return [ConversationSummary(remote_id=None, title="New Chat", is_new_chat=True)] + items
        return items

    def list_projects(self) -> list[ProjectSummary]:
        rows = self._connection.execute(
            """
            SELECT remote_id, title, href
            FROM projects
            ORDER BY sort_order ASC, rowid DESC
            """
        ).fetchall()
        return [
            ProjectSummary(remote_id=row["remote_id"], title=row["title"], href=row["href"])
            for row in rows
        ]

    def get_project(self, remote_id: str) -> ProjectSummary | None:
        row = self._connection.execute(
            "SELECT remote_id, title, href FROM projects WHERE remote_id = ?",
            (remote_id,),
        ).fetchone()
        if row is None:
            return None
        return ProjectSummary(remote_id=row["remote_id"], title=row["title"], href=row["href"])

    def replace_messages(self, remote_id: str, messages: list[Message]) -> None:
        with self._connection:
            self._connection.execute(
                "DELETE FROM messages WHERE conversation_remote_id = ?",
                (remote_id,),
            )
            for index, message in enumerate(messages):
                self._connection.execute(
                    """
                    INSERT INTO messages (conversation_remote_id, position, role, content, remote_message_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (remote_id, index, message.role, message.content, message.remote_id),
                )

    def append_message(self, remote_id: str, message: Message) -> None:
        position = self._connection.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM messages WHERE conversation_remote_id = ?",
            (remote_id,),
        ).fetchone()["next_position"]
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO messages (conversation_remote_id, position, role, content, remote_message_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (remote_id, position, message.role, message.content, message.remote_id),
            )

    def load_conversation(self, remote_id: str) -> ConversationData | None:
        row = self._connection.execute(
            "SELECT remote_id, title, updated_at, project_remote_id, href FROM conversations WHERE remote_id = ?",
            (remote_id,),
        ).fetchone()
        if not row:
            return None
        messages = [
            Message(role=message_row["role"], content=message_row["content"], remote_id=message_row["remote_message_id"])
            for message_row in self._connection.execute(
                """
                SELECT role, content, remote_message_id
                FROM messages
                WHERE conversation_remote_id = ?
                ORDER BY position ASC
                """,
                (remote_id,),
            ).fetchall()
        ]
        return ConversationData(
            summary=ConversationSummary(
                remote_id=row["remote_id"],
                title=row["title"],
                updated_at=row["updated_at"],
                project_remote_id=row["project_remote_id"],
                href=row["href"],
            ),
            messages=messages,
        )

    def set_app_state(self, key: str, value: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO app_state (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_app_state(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM app_state WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def _ensure_schema(self) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(conversations)").fetchall()
        }
        if "sort_order" not in columns:
            self._connection.execute(
                "ALTER TABLE conversations ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
            )
        if "project_remote_id" not in columns:
            self._connection.execute(
                "ALTER TABLE conversations ADD COLUMN project_remote_id TEXT"
            )
        if "href" not in columns:
            self._connection.execute(
                "ALTER TABLE conversations ADD COLUMN href TEXT"
            )
