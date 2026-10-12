"""Port for the query governor's result cache (query-governor SKILL.md)."""
from __future__ import annotations

from typing import Protocol


class Cache(Protocol):
    def get(self, key: str) -> str | None:
        ...

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        ...
