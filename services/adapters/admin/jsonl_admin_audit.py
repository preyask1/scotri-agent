# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Append-only JSONL admin audit log (OPEN_ITEMS.md #43), implementing ports.admin.AdminAuditLog.

Its own file, not a line in scotri-api.log: the api log rotates at 10 MB and mixes every request,
while this one is the record of who used admin powers, so it stays small, separate and easy to
hand to a reviewer. The path is a setting (ADMIN_AUDIT_LOG_PATH) so tests write to a temp file and
never into the real one (the mistake #56 records for the CLI audit log).
"""
from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path

from domain.admin import AdminAuditEntry


class JsonlAdminAuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def append(self, entry: AdminAuditEntry) -> None:
        line = json.dumps(entry.to_dict(), ensure_ascii=False)
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def tail(self, limit: int) -> list[dict]:
        if not self._path.is_file():
            return []
        window: deque[str] = deque(maxlen=limit)
        with self._path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                if raw.strip():
                    window.append(raw)
        out = []
        for raw in window:
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return out
