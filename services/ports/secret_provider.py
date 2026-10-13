# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Port for secret retrieval. Phase 1 adapter: env vars. Phase 2 (planned): Azure Key Vault.

Nothing outside adapters/secrets/ should ever know which of those two is in use — that's the whole
point of this port existing.
"""
from __future__ import annotations

from typing import Protocol


class SecretProvider(Protocol):
    def get(self, name: str) -> str:
        """Return the secret named `name`, or raise domain.errors.ConfigurationError if it isn't
        configured — never a bare KeyError, so a missing credential surfaces through the same
        `{"ok": false, "category": "configuration", ...}` envelope as any other deliberate failure
        instead of a raw traceback (app/cli.py's `main()` safety net only catches ScotriError)."""
        ...

    def get_optional(self, name: str, default: str | None = None) -> str | None:
        """Return the secret named `name`, or `default` if it isn't configured."""
        ...
