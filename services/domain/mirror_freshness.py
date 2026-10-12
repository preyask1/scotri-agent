"""Mirror staleness assessment (OPEN_ITEMS.md #31).

`domain/sync_policy.py` writes `last_run_at` (and per-repo `checked_at`/`ok`) into
`services/mirror/status.json` on every sync — but the mirror reader never reads it, so a read is
served identically whether the sync ran an hour ago or a fortnight ago. Combined with the fact that
nothing yet guarantees the sync process keeps running on this host, an analysis can read a stale Prod
tree and present it as current — the silent-narrowing-without-saying-so failure `agent-instructions.md`
forbids. This module turns `status.json` into a freshness verdict the mirror-read commands surface on
every read.

Advisory, not a gate (the item's own recommended default): the maintenance flag already refuses reads
during the swap *window*; an old-but-not-mid-swap mirror is still readable — a hard refusal would block
all analysis the moment the (not-yet-guaranteed) scheduler lapses. So this warns loudly and lets the
read continue; the caller decides whether the age matters for the conclusion it's about to draw.

Pure: no file IO, no clock of its own — the caller passes the parsed `status.json` dict (or None) and
`now`, so this is fully unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# One daily `0 3 * * *` cycle (24h) plus a small grace, so a single on-time daily sync never warns but
# a missed one does. Overridable via MIRROR_STALE_WARN_SECONDS (app/cli.py).
DEFAULT_WARN_AFTER_SECONDS = 90_000  # 25h


@dataclass(frozen=True)
class MirrorFreshness:
    available: bool                 # could we read a status at all?
    stale: bool                     # age exceeds the warn threshold
    warning: str | None             # a human-readable line, when something's worth flagging
    last_run_at: str | None = None
    age_seconds: int | None = None
    repo_ok: bool | None = None     # did THIS repo/branch's last sync succeed? None if no per-repo row
    repo_checked_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "stale": self.stale,
            "warning": self.warning,
            "last_run_at": self.last_run_at,
            "age_seconds": self.age_seconds,
            "repo_ok": self.repo_ok,
            "repo_checked_at": self.repo_checked_at,
        }


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def assess_freshness(
    status: dict | None,
    repo: str,
    branch: str,
    now: datetime,
    warn_after_seconds: int = DEFAULT_WARN_AFTER_SECONDS,
) -> MirrorFreshness:
    """Turn a parsed status.json (or None) into a freshness verdict for one repo/branch read."""
    if not isinstance(status, dict) or not status.get("last_run_at"):
        return MirrorFreshness(
            available=False, stale=False,
            warning="mirror freshness unknown — no status.json on this host (has the sync ever run "
                    "here?). Cannot confirm this mirror read reflects a recent Prod state.",
        )

    last_run_at = status["last_run_at"]
    # Prefer this repo/branch's own row (its checked_at/ok); fall back to the run's overall timestamp.
    repo_row = next(
        (r for r in status.get("repos", []) if r.get("repo") == repo and r.get("branch") == branch),
        None,
    )
    repo_ok = repo_row.get("ok") if repo_row else None
    repo_checked_at = (repo_row.get("checked_at") if repo_row else None) or last_run_at

    ref = _parse(repo_checked_at) or _parse(last_run_at)
    if ref is None:
        return MirrorFreshness(
            available=False, stale=False, last_run_at=last_run_at,
            warning=f"mirror freshness unknown — status.json timestamp is unparseable ({last_run_at!r}).",
            repo_ok=repo_ok, repo_checked_at=repo_checked_at,
        )

    age_seconds = int((now - ref).total_seconds())
    stale = age_seconds > warn_after_seconds

    warning: str | None = None
    if repo_ok is False:
        warning = (
            f"last sync of {repo}@{branch} FAILED (at {repo_checked_at}) — you may be reading the "
            f"prior good copy, or a tree that never fully refreshed. State this in any conclusion."
        )
    elif stale:
        warning = (
            f"mirror is stale: {repo}@{branch} last synced {age_seconds // 3600}h ago "
            f"({last_run_at}), past the {warn_after_seconds // 3600}h freshness threshold. It may not "
            f"reflect current Prod — say so if a conclusion depends on it, or wait for a fresh sync."
        )

    return MirrorFreshness(
        available=True, stale=stale, warning=warning,
        last_run_at=last_run_at, age_seconds=age_seconds,
        repo_ok=repo_ok, repo_checked_at=repo_checked_at,
    )
