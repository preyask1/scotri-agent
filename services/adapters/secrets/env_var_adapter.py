# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Phase 1 SecretProvider: plain environment variables, optionally loaded from a local .env file.

Phase 2 (planned): an AzureKeyVaultAdapter implementing the same SecretProvider port — nothing that
calls `.get(name)` needs to change when that swap happens.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from domain.errors import ConfigurationError

_DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class EnvVarSecretProvider:
    """Implements ports.secret_provider.SecretProvider."""

    def __init__(self, env_file: Path | None = _DEFAULT_ENV_FILE) -> None:
        if env_file is not None and env_file.exists():
            load_dotenv(env_file)
        # If no .env file exists (e.g. real deployment env vars are injected by the platform
        # directly), that's fine — os.environ is still consulted below.

    def get(self, name: str) -> str:
        value = os.environ.get(name)
        if value is None or value == "":
            # ConfigurationError, never a bare KeyError — app/cli.py's `main()` safety net only
            # catches ScotriError, so a bare KeyError from a missing credential used to bypass it
            # entirely and surface as a raw traceback instead of a clean {"ok": false, ...} envelope
            # (found live 2026-09-24 via app/mcp_server.py testing, tracked in OPEN_ITEMS.md, fixed here).
            raise ConfigurationError(
                f"Required environment variable '{name}' is not set. See services/.env.example "
                f"for the full list this deployment needs."
            )
        return value

    def get_optional(self, name: str, default: str | None = None) -> str | None:
        value = os.environ.get(name)
        return value if value not in (None, "") else default
