"""The one actual long-running process this codebase needs (see the hexagonal-layering discussion):
a scheduled loop that refreshes every mirrored Prod repo/branch.

Manual triggers don't need a separate code path here — `python -m app.cli sync-run-once` and the
admin panel's "Sync now" call the exact same `refresh_registry_once` this loop calls, so "scheduled"
and "manual" are guaranteed to behave identically, never two implementations that can drift apart.
All three hold the same Redis sync lock for the run; a scheduled tick that finds it held skips.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from croniter import croniter

from app.container import build_container
from app.logging_setup import configure_logging
from app.status_writer import write_status
from domain.errors import ScotriError
from domain.sync_policy import SyncInProgressError, build_status_report, refresh_registry_once

logger = logging.getLogger("scotri.sync_service")


def run_forever(cron_expression: str) -> None:
    container = build_container()
    schedule = croniter(cron_expression, start_time=time.time())

    logger.info("sync service starting — schedule: '%s'", cron_expression)
    try:  # catch up on lineage the post-swap step missed (e.g. a mirror restored or synced elsewhere)
        container.ensure_lineage_current()
    except Exception:  # noqa: BLE001
        logger.exception("lineage startup check failed; the next sync's post-swap step retries it")
    while True:
        next_run = schedule.get_next(float)
        sleep_seconds = max(0.0, next_run - time.time())
        logger.info("next scheduled refresh in %.0fs", sleep_seconds)
        time.sleep(sleep_seconds)

        try:
            outcomes = refresh_registry_once(container.registry, container.mirror_sync,
                                             container.maintenance_flag, container.sync_lock,
                                             holder="sync-service",
                                             after_swap=getattr(container, "after_mirror_swap", None),
                                             after_run=getattr(container, "after_mirror_sync", None))
        except SyncInProgressError as e:
            logger.warning("scheduled sync skipped: %s", e.user_message)
            continue
        except ScotriError as e:  # e.g. Redis down: no lock, no run, try again next tick
            logger.error("scheduled sync skipped [%s]: %s", e.category.value, e.user_message)
            continue
        write_status(container.mirror_root / "status.json", build_status_report(outcomes))

        failures = [o for o in outcomes if not o.ok]
        if failures:
            for f in failures:
                logger.error("scheduled sync: %s@%s failed [%s]: %s", f.repo, f.branch, f.category, f.message)
        else:
            logger.info("scheduled sync completed with no failures (%d repo/branch checked).", len(outcomes))


if __name__ == "__main__":
    configure_logging(log_file=Path(__file__).resolve().parents[1] / "logs" / "scotri-sync-service.log")
    container_for_default = build_container()
    default_cron = container_for_default.secrets.get_optional("SYNC_SCHEDULE_CRON", "0 3 * * *")
    run_forever(default_cron)
