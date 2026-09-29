from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class TodoStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def initialise(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _as_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "text": row["text"],
            "done": bool(row["done"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM todos ORDER BY done ASC, created_at DESC"
            ).fetchall()
        return [self._as_dict(row) for row in rows]

    def create(self, text: str) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO todos (text, done, created_at, updated_at) VALUES (?, 0, ?, ?)",
                (text, now, now),
            )
            row = connection.execute(
                "SELECT * FROM todos WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._as_dict(row)

    def update(
        self, todo_id: int, *, text: str | None = None, done: bool | None = None
    ) -> dict[str, Any] | None:
        assignments: list[str] = []
        values: list[Any] = []
        if text is not None:
            assignments.append("text = ?")
            values.append(text)
        if done is not None:
            assignments.append("done = ?")
            values.append(int(done))
        if not assignments:
            return self.get(todo_id)

        assignments.append("updated_at = ?")
        values.append(datetime.now(UTC).isoformat())
        values.append(todo_id)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE todos SET {', '.join(assignments)} WHERE id = ?", values
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM todos WHERE id = ?", (todo_id,)
            ).fetchone()
        return self._as_dict(row)

    def get(self, todo_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM todos WHERE id = ?", (todo_id,)
            ).fetchone()
        return self._as_dict(row) if row else None

    def delete(self, todo_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
        return cursor.rowcount > 0

