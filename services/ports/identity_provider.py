# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Port for validating a frontend-supplied identity token into a trusted Principal.

OPEN_ITEMS.md (2026-09-22): platform is read from the token's own claims (appid/azp), never a
self-reported header a caller could forge.
"""
from __future__ import annotations

from typing import Protocol

from domain.models import Principal


class IdentityProvider(Protocol):
    def validate(self, token: str) -> Principal:
        """Raise domain.errors.CredentialError for an invalid, expired, or wrong-audience token —
        never return a Principal for a token that didn't actually verify."""
        ...
