"""Dev/testing-only IdentityProvider: no Entra call at all (OPEN_ITEMS.md-style decision, 2026-09-25).

Only ever wired in when SCOTRI_AUTH_MODE=dev (see app/container.py) — never a fallback for a
misconfigured prod deployment. Still requires a shared static bearer token (SCOTRI_DEV_AUTH_TOKEN),
compared with a constant-time check, so a test frontend must present *something* rather than any
request being accepted outright; the Principal returned is otherwise fixed/configurable via env vars,
never derived from the token itself since there is no real claims to read.
"""
from __future__ import annotations

import hmac

from domain.errors import CredentialError
from domain.models import Principal
from ports.secret_provider import SecretProvider


class StaticIdentityProvider:
    """Implements ports.identity_provider.IdentityProvider. Dev/testing use only."""

    def __init__(self, secrets: SecretProvider) -> None:
        self._expected_token = secrets.get("SCOTRI_DEV_AUTH_TOKEN")
        self._subject = secrets.get_optional("SCOTRI_DEV_PRINCIPAL_SUBJECT", "dev-user")
        self._display_name = secrets.get_optional("SCOTRI_DEV_PRINCIPAL_NAME", "Dev User")
        self._platform = secrets.get_optional("SCOTRI_DEV_PRINCIPAL_PLATFORM", "dev-frontend")
        roles = secrets.get_optional("SCOTRI_DEV_PRINCIPAL_ROLES", "")
        self._roles = [r.strip() for r in roles.split(",") if r.strip()]

    def validate(self, token: str) -> Principal:
        if not hmac.compare_digest(token, self._expected_token):
            raise CredentialError("Dev bearer token did not match SCOTRI_DEV_AUTH_TOKEN")
        return Principal(
            subject=self._subject,
            display_name=self._display_name,
            platform=self._platform,
            roles=list(self._roles),
        )
