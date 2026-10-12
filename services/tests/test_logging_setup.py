"""OPEN_ITEMS.md #56: the test suite must never write into the real services/logs/ audit trail."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.logging_setup import configure_logging, resolve_log_file

SERVICES_ROOT = Path(__file__).resolve().parents[1]
REAL_LOGS = SERVICES_ROOT / "logs"


def test_log_dir_unset_keeps_the_default(monkeypatch):
    monkeypatch.delenv("SCOTRI_LOG_DIR", raising=False)
    assert resolve_log_file(REAL_LOGS / "scotri-cli.log") == REAL_LOGS / "scotri-cli.log"


def test_log_dir_moves_the_file_and_keeps_its_name(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOTRI_LOG_DIR", str(tmp_path))
    assert resolve_log_file(REAL_LOGS / "scotri-api.log") == tmp_path / "scotri-api.log"


def test_relative_log_dir_resolves_under_services(monkeypatch):
    monkeypatch.setenv("SCOTRI_LOG_DIR", "./elsewhere")
    assert resolve_log_file(Path("x/scotri-cli.log")) == SERVICES_ROOT / "elsewhere" / "scotri-cli.log"


def test_suite_logging_never_targets_the_real_logs_folder():
    # Whichever entry point configured logging first in this run, its file must not be services/logs/.
    configure_logging(log_file=REAL_LOGS / "scotri-cli.log")  # idempotent; configures if nothing has
    files = [Path(h.baseFilename) for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
    assert files, "expected a file handler"
    for f in files:
        assert REAL_LOGS.resolve() not in f.resolve().parents, f
