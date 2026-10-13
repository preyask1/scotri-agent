# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Pattern-based secret redaction — the code half of the "never store or echo a secret" hard rule
(agent-instructions.md), applied wherever text could reach a caller's context or a file (OPEN_ITEMS.md
#34).

Deliberately over-eager: a false redaction costs a glance at the source, a missed one leaks a
credential. Pattern-based, so it catches a third party's credential embedded in a mirror notebook or a
query row — not just this process's own env-var values (that's what app/logging_setup.py already does
for log lines). It is NOT a proof: a bare high-entropy string with no `key=` context can still slip
through, which is why this pairs with tool-restriction (OPEN_ITEMS.md #28), not a substitute for it.

Pure: no IO. domain/trace_policy.py imports redact_secrets from here (its original home), and
app/cli.py runs every printed payload through it.
"""
from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

# `NAME=value` / `NAME: value` / `"name": "value"` where NAME says it's a secret. Covers the common
# credential-assignment shapes AND the Azure/connection-string keys SQL-migration notebooks carry
# (AccountKey, SharedAccessKey, SAS `sig`, connection-string Password/Pwd) — OPEN_ITEMS.md #34.
# `sig` (the SAS signature parameter) only as a whole segment of the key (`sig`, `x-sig`,
# `url_signature`): as a bare substring it also matched `designation`, `assignment` and `signal`, and
# "Designation" is a core field-force dimension in tickets and notebooks here.
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<key>
        [\w.-]*(?:
            secret | token | password | passwd | pwd | api[_-]?key | access[_-]?key |
            client[_-]?secret | credential | accountkey | sharedaccesskey
        )[\w.-]*
      | (?<![A-Za-z0-9])(?:[\w.-]*[_.-])?sig(?:nature)?(?:[_.-][\w.-]*)?(?![A-Za-z0-9])
    )
    (?P<sep>["']?\s*[:=]\s*["']?)
    (?P<value>[^\s"',;}]{4,})
    """
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
# sk-* API keys, JWTs (eyJ...header.payload.sig), Slack xox*-* tokens.
_KNOWN_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,})"
)


def redact_secrets(text: str) -> str:
    """Replace credential-shaped substrings with [REDACTED], preserving the key so the reader still
    sees WHAT was redacted (e.g. `AccountKey=[REDACTED]`), just not the value. Safe to run on a
    serialized JSON string — [REDACTED] contains no JSON-significant characters, so structure holds."""
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group('key')}{m.group('sep')}{_REDACTED}", text)
    text = _BEARER.sub(f"Bearer {_REDACTED}", text)
    return _KNOWN_TOKEN.sub(_REDACTED, text)
