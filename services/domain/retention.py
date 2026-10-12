"""How long Scotri keeps the files it stores (OPEN_ITEMS.md #6(a)).

Three kinds of file pile up, and before this nothing deleted any of them:

- uploads (`uploads/<upload_id>/<name>` in the artifact store), the files callers attach to prompts;
- published deliverables (`engagements/<job_id>/<path>`), the markdown and patches a run wrote;
- staged copies of uploads in the workspace (`scratch/uploads/<job_id>/`), made for the agent to read.

Retention is off unless configured: UPLOAD_RETENTION_DAYS covers uploads and their staged copies,
SHARED_FILE_RETENTION_DAYS covers published deliverables. Empty or 0 keeps files forever, which was
the only behaviour before. The purge runs only when an operator runs `artifacts-purge` (dry run unless
--confirmed); engagements can't run it (domain/tool_scope.py), and it is never scheduled on a local
instance. On Azure the same two numbers belong in Blob Storage lifecycle-management rules on the
`uploads/` and `engagements/` prefixes, which delete without any process running.

A deleted deliverable is gone: GET .../files/{path} for it returns 404 afterwards, and the history
record that pointed at it stays. That is the trade the retention period encodes.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from domain.errors import ConfigurationError
from domain.shared_files import UPLOADS_DIR
from ports.artifact_store import ArtifactStore

UPLOADS_PREFIX = "uploads/"
SHARED_PREFIX = "engagements/"


@dataclass(frozen=True)
class RetentionPolicy:
    upload_days: int | None = None
    """Uploads and their staged copies older than this are purged. None = keep forever."""
    shared_file_days: int | None = None
    """Published deliverables older than this are purged. None = keep forever."""

    @property
    def enabled(self) -> bool:
        return self.upload_days is not None or self.shared_file_days is not None


def parse_days(name: str, raw: str | None) -> int | None:
    """A retention setting: empty or 0 means keep forever (None); a positive whole number of days
    otherwise. Anything else is a configuration error, never silently read as "delete now"."""
    value = (raw or "").strip()
    if not value or value == "0":
        return None
    if not value.isdigit():
        raise ConfigurationError(f"{name} must be a whole number of days (empty or 0 = keep forever), got {raw!r}")
    return int(value)


@dataclass
class PurgeReport:
    dry_run: bool
    upload_cutoff: str | None = None
    shared_file_cutoff: str | None = None
    uploads: list[str] = field(default_factory=list)
    shared_files: list[str] = field(default_factory=list)
    staged_dirs: list[str] = field(default_factory=list)
    bytes_freed: int = 0

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run, "upload_cutoff": self.upload_cutoff,
            "shared_file_cutoff": self.shared_file_cutoff,
            "counts": {"uploads": len(self.uploads), "shared_files": len(self.shared_files),
                       "staged_dirs": len(self.staged_dirs)},
            "bytes_freed": self.bytes_freed,
            "uploads": self.uploads, "shared_files": self.shared_files, "staged_dirs": self.staged_dirs,
        }


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _purge_prefix(store: ArtifactStore, prefix: str, cutoff: datetime, dry_run: bool,
                  out: list[str], report: PurgeReport) -> None:
    for record in store.list(prefix):
        if _aware(record.created_at) < cutoff:
            if dry_run or store.delete(record.key):
                out.append(record.key)
                report.bytes_freed += record.size


def _dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def purge_expired(store: ArtifactStore, workspace_root: str | Path, policy: RetentionPolicy,
                  now: datetime | None = None, dry_run: bool = True) -> PurgeReport:
    """Delete (or, with dry_run, only list) everything older than the policy allows."""
    now = _aware(now or datetime.now(timezone.utc))
    report = PurgeReport(dry_run=dry_run)

    if policy.upload_days is not None:
        cutoff = now - timedelta(days=policy.upload_days)
        report.upload_cutoff = cutoff.isoformat()
        _purge_prefix(store, UPLOADS_PREFIX, cutoff, dry_run, report.uploads, report)
        staged_root = Path(workspace_root).joinpath(*UPLOADS_DIR.parts)
        if staged_root.is_dir():
            for job_dir in sorted(staged_root.iterdir()):
                if not job_dir.is_dir() or job_dir.is_symlink():
                    continue
                if datetime.fromtimestamp(job_dir.stat().st_mtime, timezone.utc) < cutoff:
                    report.bytes_freed += _dir_size(job_dir)
                    if not dry_run:
                        shutil.rmtree(job_dir)
                    report.staged_dirs.append(job_dir.relative_to(workspace_root).as_posix())

    if policy.shared_file_days is not None:
        cutoff = now - timedelta(days=policy.shared_file_days)
        report.shared_file_cutoff = cutoff.isoformat()
        _purge_prefix(store, SHARED_PREFIX, cutoff, dry_run, report.shared_files, report)

    return report
