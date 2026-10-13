# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Azure AD (Entra ID) token validation (OPEN_ITEMS.md, 2026-09-22).

Single API app registration is the token audience; every frontend (Teams, a web portal, ...) is its
own app registration acquiring a token for THIS audience, so platform is read from the token's own
appid/azp claim — never a self-reported header a caller could forge.
"""
from __future__ import annotations

import jwt

from domain.errors import CredentialError
from domain.models import Principal
from ports.secret_provider import SecretProvider

_ALGORITHMS = ["RS256"]


class AzureAdIdentityProvider:
    """Implements ports.identity_provider.IdentityProvider."""

    def __init__(self, secrets: SecretProvider) -> None:
        tenant_id = secrets.get("AZURE_AD_TENANT_ID")
        self._audience = secrets.get("AZURE_AD_API_AUDIENCE")
        self._issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        jwks_uri = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        self._jwks_client = jwt.PyJWKClient(jwks_uri)

    def validate(self, token: str) -> Principal:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=_ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
            )
        except jwt.PyJWTError as exc:
            raise CredentialError(f"Azure AD token did not validate: {exc}") from None

        # appid (v1) / azp (v2, "authorized party") identifies the CALLING application — this is
        # what "platform" means here, deliberately never taken from a request header.
        platform = claims.get("azp") or claims.get("appid") or "unknown"
        return Principal(
            subject=claims.get("oid") or claims["sub"],
            display_name=claims.get("name", claims.get("preferred_username", "unknown")),
            platform=platform,
            roles=list(claims.get("roles", [])),
        )
