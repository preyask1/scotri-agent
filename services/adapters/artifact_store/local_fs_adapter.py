"""ArtifactStore on the local filesystem — the Mac/Windows default (ARTIFACT_STORE=local).

Layout under the root: `blobs/<key>` holds the bytes and `meta/<key>.json` holds the record, in two
separate trees so no stored name can ever be mistaken for a metadata file. Writes go to a temp file
and are swapped in with os.replace, which is atomic on both POSIX and Windows, so a reader never sees
a half-written file.

Single-host only: a second API replica on another machine would not see these files. That is what
the Azure Blob adapter is for (adapters/artifact_store/azure_blob_adapter.py).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from domain.errors import PolicyError
from ports.artifact_store import StoredFile

_SEGMENT = re.compile(r"^[A-Za-z0-9._-][A-Za-z0-9 ._()+-]*$")


def _check_key(key: str) -> list[str]:
    """Defense in depth: domain/shared_files.py already builds safe keys, but a key that could
    resolve outside the root must be impossible here regardless of who calls."""
    parts = key.split("/")
    if not key or key.startswith("/") or any(p in ("", ".", "..") or not _SEGMENT.match(p) for p in parts):
        raise PolicyError(f"invalid storage key: {key!r}")
    return parts


class LocalFsArtifactStore:
    """Implements ports.artifact_store.ArtifactStore."""

    def __init__(self, root: Path) -> None:
        self._blobs = root / "blobs"
        self._meta = root / "meta"

    def _paths(self, key: str) -> tuple[Path, Path]:
        parts = _check_key(key)
        return self._blobs.joinpath(*parts), self._meta.joinpath(*parts[:-1], parts[-1] + ".json")

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def put(self, key: str, data: bytes, content_type: str, metadata: dict[str, str] | None = None) -> StoredFile:
        blob_path, meta_path = self._paths(key)
        record = StoredFile(
            key=key, size=len(data), content_type=content_type,
            created_at=datetime.now(timezone.utc), metadata=dict(metadata or {}),
        )
        self._atomic_write(blob_path, data)
        self._atomic_write(meta_path, json.dumps({
            "size": record.size, "content_type": record.content_type,
            "created_at": record.created_at.isoformat(), "metadata": record.metadata,
        }).encode("utf-8"))
        return record

    def head(self, key: str) -> StoredFile | None:
        blob_path, meta_path = self._paths(key)
        if not blob_path.is_file() or not meta_path.is_file():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return StoredFile(
            key=key, size=meta["size"], content_type=meta["content_type"],
            created_at=datetime.fromisoformat(meta["created_at"]), metadata=meta.get("metadata", {}),
        )

    def get(self, key: str) -> tuple[bytes, StoredFile] | None:
        record = self.head(key)
        if record is None:
            return None
        blob_path, _ = self._paths(key)
        return blob_path.read_bytes(), record

    def delete(self, key: str) -> bool:
        blob_path, meta_path = self._paths(key)
        existed = blob_path.is_file()
        blob_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        for base, path in ((self._blobs, blob_path), (self._meta, meta_path)):
            parent = path.parent  # drop the folders the key leaves empty, never the root itself
            while parent != base and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        return existed

    def list(self, prefix: str) -> list[StoredFile]:
        # Walk only the directory the prefix names, so listing one job never scans the whole store.
        directory = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        base = self._blobs.joinpath(*_check_key(directory)) if directory else self._blobs
        if not base.is_dir():
            return []
        records = []
        for path in base.rglob("*"):
            if not path.is_file() or path.name.startswith(".tmp-"):
                continue
            key = path.relative_to(self._blobs).as_posix()
            if key.startswith(prefix):
                record = self.head(key)
                if record is not None:
                    records.append(record)
        return sorted(records, key=lambda r: r.key)
