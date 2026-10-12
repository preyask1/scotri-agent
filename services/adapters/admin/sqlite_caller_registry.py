"""SQLite caller-app register (OPEN_ITEMS.md #43), implementing ports.admin.CallerRegistry.

Kept in the same database file as engagement history, so it moves with it when #15 picks the
production store. Metadata only: an app id, a name, a kind, an owner, notes. Never a credential.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from domain.admin import CallerApp, CallerKind

_SCHEMA = """
CREATE TABLE IF NOT EXISTS caller_apps (
    app_id        TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    owner         TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    registered_by TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""


class SqliteCallerRegistry:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row(row: sqlite3.Row) -> CallerApp:
        return CallerApp(
            app_id=row["app_id"], name=row["name"], kind=CallerKind(row["kind"]),
            owner=row["owner"], notes=row["notes"], registered_by=row["registered_by"],
            registered_at=datetime.fromisoformat(row["registered_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list(self) -> list[CallerApp]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM caller_apps ORDER BY app_id").fetchall()
        return [self._row(r) for r in rows]

    def get(self, app_id: str) -> CallerApp | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM caller_apps WHERE app_id = ?", (app_id,)).fetchone()
        return self._row(row) if row else None

    def upsert(self, app: CallerApp) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO caller_apps
                   (app_id, name, kind, owner, notes, registered_by, registered_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(app_id) DO UPDATE SET
                     name=excluded.name, kind=excluded.kind, owner=excluded.owner,
                     notes=excluded.notes, updated_at=excluded.updated_at""",
                (app.app_id, app.name, app.kind.value, app.owner, app.notes, app.registered_by,
                 app.registered_at.isoformat(), app.updated_at.isoformat()),
            )
            conn.commit()

    def delete(self, app_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM caller_apps WHERE app_id = ?", (app_id,))
            conn.commit()
        return cur.rowcount > 0
