"""Port for storing files Scotri shares or receives: the markdown deliverables an engagement writes
(intake, verdict, impact analysis, plan, test results, handoff, proposal patches) and files a user
uploads to attach to a prompt.

A flat key/value blob store with prefix listing — the smallest shape both a local folder (Mac and
Windows today) and Azure Blob Storage (the Azure/Foundry deployment) implement natively, so moving
between them is a container.py setting, not a code change. Keys are "/"-separated and never start
with "/"; domain/shared_files.py owns the key layout.

This store is Scotri's own storage. It never holds or touches product-repo content's write path:
nothing read from it is ever pushed anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class StoredFile:
    key: str
    size: int
    content_type: str
    created_at: datetime
    metadata: dict[str, str] = field(default_factory=dict)
    """Small string metadata (owner subject, original filename, ...). Values are plain strings; an
    adapter that needs a restricted alphabet (Azure Blob metadata is ASCII) encodes and decodes them
    itself, so callers never see the difference."""


class ArtifactStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str, metadata: dict[str, str] | None = None) -> StoredFile:
        """Store `data` under `key`, replacing anything already there."""
        ...

    def get(self, key: str) -> tuple[bytes, StoredFile] | None:
        """The bytes and their record, or None when nothing is stored under `key`."""
        ...

    def head(self, key: str) -> StoredFile | None:
        """The record alone, without reading the bytes."""
        ...

    def list(self, prefix: str) -> list[StoredFile]:
        """Every file whose key starts with `prefix`, ordered by key."""
        ...

    def delete(self, key: str) -> bool:
        """Remove the file under `key`. True if something was removed, False if nothing was there.
        Used only by the retention purge (domain/retention.py, OPEN_ITEMS.md #6(a))."""
        ...
