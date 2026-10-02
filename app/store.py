from __future__ import annotations

import sqlite3
import json
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS stocks (
                    symbol TEXT PRIMARY KEY COLLATE NOCASE,
                    label TEXT NOT NULL,
                    currency TEXT NOT NULL DEFAULT '',
                    sort_order INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS climate_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    measurement TEXT NOT NULL,
                    field TEXT NOT NULL,
                    label TEXT NOT NULL,
                    unit TEXT NOT NULL DEFAULT '',
                    decimals INTEGER NOT NULL DEFAULT 1,
                    tags TEXT NOT NULL DEFAULT '{}',
                    sort_order INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(measurement, field, tags)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS deadlines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    due TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'Deadline',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    start TEXT NOT NULL,
                    ends_at TEXT,
                    location TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def get_weather_location(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_meta WHERE key = 'weather_location'"
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def set_weather_location(self, location: dict[str, Any]) -> dict[str, Any]:
        value = json.dumps(location, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO app_meta (key, value) VALUES ('weather_location', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (value,),
            )
        return location

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

    def _seed_once(self, key: str, callback: Any) -> None:
        with self._connect() as connection:
            seeded = connection.execute(
                "SELECT 1 FROM app_meta WHERE key = ?", (key,)
            ).fetchone()
            if seeded:
                return
            callback(connection)
            connection.execute(
                "INSERT INTO app_meta (key, value) VALUES (?, ?)", (key, "1")
            )

    def seed_stocks(self, items: list[dict[str, Any]]) -> None:
        now = datetime.now(UTC).isoformat()

        def seed(connection: sqlite3.Connection) -> None:
            for index, item in enumerate(items):
                symbol = str(item.get("symbol", "")).strip().upper()
                if not symbol:
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO stocks
                        (symbol, label, currency, sort_order, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        symbol,
                        str(item.get("label") or symbol).strip(),
                        str(item.get("currency", "")).strip().upper(),
                        index,
                        now,
                    ),
                )

        self._seed_once("stocks_seeded_v1", seed)

    def list_stocks(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT symbol, label, currency FROM stocks ORDER BY sort_order, created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def add_stock(self, symbol: str, label: str, currency: str = "") -> dict[str, Any]:
        symbol = symbol.strip().upper()
        label = label.strip() or symbol
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            next_order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM stocks"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO stocks (symbol, label, currency, sort_order, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    label = excluded.label,
                    currency = CASE WHEN excluded.currency = '' THEN stocks.currency ELSE excluded.currency END
                """,
                (symbol, label, currency.strip().upper(), next_order, now),
            )
            row = connection.execute(
                "SELECT symbol, label, currency FROM stocks WHERE symbol = ?", (symbol,)
            ).fetchone()
        return dict(row)

    def delete_stock(self, symbol: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM stocks WHERE symbol = ?", (symbol.strip().upper(),)
            )
        return cursor.rowcount > 0

    def seed_climate_metrics(self, items: list[dict[str, Any]]) -> None:
        now = datetime.now(UTC).isoformat()

        def seed(connection: sqlite3.Connection) -> None:
            for index, item in enumerate(items):
                measurement = str(item.get("measurement", "")).strip()
                field = str(item.get("field", "")).strip()
                if not measurement or not field:
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO climate_metrics
                        (measurement, field, label, unit, decimals, tags, sort_order, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        measurement,
                        field,
                        str(item.get("label") or field).strip(),
                        str(item.get("unit", "")).strip(),
                        int(item.get("decimals", 1)),
                        json.dumps(item.get("tags", {}), sort_keys=True),
                        index,
                        now,
                    ),
                )

        self._seed_once("climate_metrics_seeded_v1", seed)

    @staticmethod
    def _climate_metric(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "measurement": row["measurement"],
            "field": row["field"],
            "label": row["label"],
            "unit": row["unit"],
            "decimals": row["decimals"],
            "tags": json.loads(row["tags"] or "{}"),
        }

    def list_climate_metrics(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM climate_metrics ORDER BY sort_order, created_at"
            ).fetchall()
        return [self._climate_metric(row) for row in rows]

    def add_climate_metric(
        self,
        measurement: str,
        field: str,
        label: str,
        unit: str = "",
        decimals: int = 1,
        tags: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        tag_json = json.dumps(tags or {}, sort_keys=True)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            next_order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM climate_metrics"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO climate_metrics
                    (measurement, field, label, unit, decimals, tags, sort_order, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(measurement, field, tags) DO UPDATE SET
                    label = excluded.label,
                    unit = excluded.unit,
                    decimals = excluded.decimals
                """,
                (
                    measurement.strip(),
                    field.strip(),
                    label.strip() or field.strip(),
                    unit.strip(),
                    decimals,
                    tag_json,
                    next_order,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM climate_metrics
                WHERE measurement = ? AND field = ? AND tags = ?
                """,
                (measurement.strip(), field.strip(), tag_json),
            ).fetchone()
        return self._climate_metric(row)

    def delete_climate_metric(self, metric_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM climate_metrics WHERE id = ?", (metric_id,)
            )
        return cursor.rowcount > 0

    @staticmethod
    def _deadline(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "due": row["due"],
            "kind": row["kind"],
        }

    def seed_deadlines(self, items: list[dict[str, Any]]) -> None:
        now = datetime.now(UTC).isoformat()

        def seed(connection: sqlite3.Connection) -> None:
            for item in items:
                title = str(item.get("title", "")).strip()
                due = str(item.get("due", "")).strip()
                if not title or not due:
                    continue
                connection.execute(
                    "INSERT INTO deadlines (title, due, kind, created_at) VALUES (?, ?, ?, ?)",
                    (title, due, str(item.get("kind") or "Deadline").strip(), now),
                )

        self._seed_once("deadlines_seeded_v1", seed)

    def list_deadlines(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM deadlines ORDER BY due, created_at"
            ).fetchall()
        return [self._deadline(row) for row in rows]

    def add_deadline(
        self, title: str, due: str, kind: str = "Deadline"
    ) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO deadlines (title, due, kind, created_at) VALUES (?, ?, ?, ?)",
                (title.strip(), due, kind.strip() or "Deadline", now),
            )
            row = connection.execute(
                "SELECT * FROM deadlines WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._deadline(row)

    def delete_deadline(self, deadline_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM deadlines WHERE id = ?", (deadline_id,)
            )
        return cursor.rowcount > 0

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "start": row["start"],
            "end": row["ends_at"],
            "location": row["location"],
        }

    def seed_events(self, items: list[dict[str, Any]]) -> None:
        now = datetime.now(UTC).isoformat()

        def seed(connection: sqlite3.Connection) -> None:
            for item in items:
                title = str(item.get("title", "")).strip()
                start = str(item.get("start", "")).strip()
                if not title or not start:
                    continue
                connection.execute(
                    """
                    INSERT INTO events (title, start, ends_at, location, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        title,
                        start,
                        str(item.get("end") or "").strip() or None,
                        str(item.get("location") or "").strip(),
                        now,
                    ),
                )

        self._seed_once("events_seeded_v1", seed)

    def list_events(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY start, created_at"
            ).fetchall()
        return [self._event(row) for row in rows]

    def add_event(
        self,
        title: str,
        start: str,
        end: str | None = None,
        location: str = "",
    ) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO events (title, start, ends_at, location, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (title.strip(), start, end, location.strip(), now),
            )
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._event(row)

    def delete_event(self, event_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM events WHERE id = ?", (event_id,))
        return cursor.rowcount > 0

