# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""The read-only statement guard for databricks-query (agent-instructions.md, databricks-query
SKILL.md).

Deliberately pure and dependency-free: this is a business rule ("this agent never writes"), not a
Databricks-specific concern, so it lives in the domain layer and the Databricks adapter calls into
it before ever reaching the SDK. Defense in depth — the service-principal's SELECT-only grant is the
platform-side backstop; this is the application-side one, and both must independently hold.

Three layers, applied in order, each of which must pass:

1. **An allowlist on the leading keyword.** Only a statement that *starts* as a read can run. This
   is the layer that matters: it was added 2026-09-27 after a gap review found that the guard was a
   denylist alone, and a denylist has to enumerate every destructive verb Databricks will ever add.
   It missed `VACUUM` (permanently deletes Delta files beyond the retention window) and `RESTORE
   TABLE ... VERSION AS OF` (rolls a table back to an earlier version) — both real mutations, both
   of which passed the guard. An allowlist inverts the burden: a verb nobody here has thought about
   is refused by default instead of admitted by default.
2. **A single-statement check**, so `SELECT 1; DROP TABLE t` cannot ride in behind an allowed
   leading keyword.
3. **The original forbidden-keyword denylist**, kept as a second layer over the whole statement, so
   a write verb buried in a subquery still fails closed even though layer 1 already passed.
"""
from __future__ import annotations

import re

from domain.models import QueryNotAllowedError

# Layer 1. The only statements this agent may ever start. `SELECT`/`DESCRIBE`/`SHOW` are what
# databricks-query SKILL.md documents; `DESC` is Databricks' own accepted abbreviation of DESCRIBE,
# and `WITH` is a CTE, which can only resolve to a SELECT here (Databricks has no writing CTE) and
# is needed for any non-trivial test-plan assertion. Nothing else — deliberately NOT `EXPLAIN`,
# which no skill documents; widening this tuple is a decision to be made and recorded, not a
# convenience to reach for when a query is refused.
ALLOWED_LEADING_KEYWORDS = ("SELECT", "WITH", "DESCRIBE", "DESC", "SHOW")

# Layer 3. Word-boundary, case-insensitive. Order doesn't matter; this is a membership check, not a
# parser. Everything from `VACUUM` down was added 2026-09-27 alongside the allowlist above — the
# allowlist already refuses each of them as a leading keyword, so these entries exist to catch the
# same verb appearing later in an otherwise allowed statement.
FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "MERGE",
    "CREATE", "DROP", "ALTER", "TRUNCATE",
    "COPY INTO", "GRANT", "REVOKE", "DENY",
    "EXECUTE", "CALL",
    "VACUUM", "RESTORE", "OPTIMIZE", "ZORDER", "ANALYZE",
    "REFRESH", "MSCK REPAIR",
    "CACHE TABLE", "UNCACHE TABLE", "CLEAR CACHE",
    "COMMENT ON", "SET TAG", "UNSET TAG",
)

_FORBIDDEN_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in FORBIDDEN_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LEADING_WORD = re.compile(r"[A-Za-z_]+")

_SEE_DOCS = "See agent-instructions.md."


def _strip_comments(statement: str) -> str:
    """Remove SQL comments, so a leading comment doesn't hide the real first keyword and a `;`
    inside a comment doesn't read as a second statement.

    Used ONLY for layers 1 and 2. The denylist in layer 3 deliberately still runs against the
    original text: a forbidden keyword sitting in a comment is not dangerous, but refusing it
    anyway keeps the guard failing closed, which is the behavior this module has always had.
    """
    return _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", statement))


def assert_select_only(statement: str) -> None:
    """Raise QueryNotAllowedError unless `statement` is a single, read-shaped statement.

    Deliberately does NOT try to be a full SQL parser. A false positive (e.g. a forbidden word
    appearing inside a string literal) is an acceptable failure mode here: it fails closed, which
    is what we want. If that ever blocks a genuinely safe query, that's a sign the query should be
    rewritten, not a sign this guard should be loosened.
    """
    uncommented = _strip_comments(statement).strip()

    # Layer 1 — the leading keyword must be one this agent is allowed to run at all.
    leading = _LEADING_WORD.match(uncommented.lstrip("( \t\r\n"))
    if leading is None:
        raise QueryNotAllowedError(
            f"Statement does not begin with a keyword — this agent runs "
            f"{'/'.join(ALLOWED_LEADING_KEYWORDS)} statements only. {_SEE_DOCS}"
        )
    if leading.group(0).upper() not in ALLOWED_LEADING_KEYWORDS:
        raise QueryNotAllowedError(
            f"Statement starts with '{leading.group(0).upper()}' — this agent runs "
            f"{'/'.join(ALLOWED_LEADING_KEYWORDS)} statements only, always, no exceptions. "
            f"{_SEE_DOCS}"
        )

    # Layer 2 — one statement, so nothing rides in behind the allowed leading keyword.
    head, _, tail = uncommented.partition(";")
    if tail.strip():
        raise QueryNotAllowedError(
            "Statement contains more than one statement — this agent runs exactly one read "
            f"statement per call, so a trailing `;{tail.strip()[:40]}` is refused. {_SEE_DOCS}"
        )
    del head

    # Layer 3 — the original denylist, over the whole (uncollapsed, uncommented-inclusive) text.
    # Whitespace is collapsed first so a multi-word entry still matches when it was written with a
    # line break or a double space between its words (`COPY  INTO`, `COMMENT\nON`).
    match = _FORBIDDEN_PATTERN.search(re.sub(r"\s+", " ", statement))
    if match:
        raise QueryNotAllowedError(
            f"Statement contains forbidden keyword '{match.group(1).upper()}' — this agent is "
            f"{'/'.join(ALLOWED_LEADING_KEYWORDS)} only, always, no exceptions. {_SEE_DOCS}"
        )


def canonicalize_for_cache(statement: str) -> str:
    """Normalize a SQL/DAX statement for cache-key purposes (query-governor SKILL.md).

    Collapses whitespace and lowercases OUTSIDE quoted literals — good enough to catch "the same
    query, differently formatted" without needing a real SQL/DAX parser. A quoted literal is kept
    byte for byte: Databricks compares strings case-sensitively, so `WHERE r = 'North'` and
    `WHERE r = 'NORTH'` are different queries and must never share a cache entry (they did, until
    2026-10-11). An unterminated quote keeps the rest of the text as-is, which only ever makes two
    keys more distinct, never less.
    """
    parts = _QUOTED_OR_NOT.findall(statement)
    out = [p if p[:1] in "'\"`" else re.sub(r"\s+", " ", p).lower() for p in parts]
    return "".join(out).strip()


# A single-, double- or backtick-quoted run (doubled quote = escaped quote, as SQL and DAX write
# it; a backslash escape is accepted too), an unterminated quote to the end, or a run of anything
# else.
_QUOTED_OR_NOT = re.compile(
    r"'(?:[^'\\]|\\.|'')*(?:'|$)|\"(?:[^\"\\]|\\.|\"\")*(?:\"|$)|`(?:[^`]|``)*(?:`|$)|[^'\"`]+",
    re.DOTALL,
)
