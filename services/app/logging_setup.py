"""Structured, persistent, secret-redacting logging — one call at the top of every entry point.

Before this existed, `cli.py` and `sync_service.py` only logged to the console via
`logging.basicConfig`, which vanishes the moment the terminal closes — useless for a scheduled job
running unattended on a server. This also generalizes the redaction the git adapter needed on its
own (2026-09-17's PAT leak) to EVERY log line, from every adapter, automatically — not just git's.
"""
from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_REDACTED = "***REDACTED***"

# Redact the VALUE of any environment variable whose NAME looks secret-like — a general safety
# net, not an enumerated list of "the secrets we remembered to name." This is what would have
# caught the PAT leak even before the git-adapter-specific fix existed.
_SECRET_NAME_PATTERN = re.compile(r"(SECRET|TOKEN|PASSWORD|_PAT\b|PAT_)", re.IGNORECASE)


def _collect_secret_values() -> list[str]:
    return sorted(
        {v for k, v in os.environ.items() if v and _SECRET_NAME_PATTERN.search(k)},
        key=len,
        reverse=True,  # redact longer values first, in case one secret is a substring of another
    )


class RedactSecretsFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for secret in self._secrets:
            if secret in msg:
                msg = msg.replace(secret, _REDACTED)
        record.msg = msg
        record.args = ()
        return True


def resolve_log_file(log_file: Path) -> Path:
    """`SCOTRI_LOG_DIR`, when set, moves every scotri-*.log into that folder (same file name). The
    test suite sets it to a temp folder (tests/conftest.py) so a pytest run never appends fake
    `governed_write` lines to the real audit trail (OPEN_ITEMS.md #56). A relative value resolves
    under services/, like container.path_setting."""
    value = os.environ.get("SCOTRI_LOG_DIR")
    if not value:
        return log_file
    folder = Path(value).expanduser()
    if not folder.is_absolute():
        folder = Path(__file__).resolve().parents[1] / folder
    return folder / log_file.name


def configure_logging(log_file: Path | None = None, level: int = logging.INFO) -> None:
    """Call once, at the top of main()/`__main__`. Idempotent — safe to call more than once."""
    root = logging.getLogger()
    if getattr(root, "_scotri_configured", False):
        return

    # Load .env into os.environ FIRST — the redaction filter below scans os.environ for
    # secret-shaped values, and would silently miss everything from .env if this ran after it.
    from adapters.secrets.env_var_adapter import EnvVarSecretProvider

    EnvVarSecretProvider()

    root.setLevel(level)
    redact_filter = RedactSecretsFilter(_collect_secret_values())
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(redact_filter)
    root.addHandler(console)

    if log_file is not None:
        log_file = resolve_log_file(log_file)  # after .env is loaded, so .env can set SCOTRI_LOG_DIR
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redact_filter)
        root.addHandler(file_handler)

    root._scotri_configured = True  # type: ignore[attr-defined]
