# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Grounding check: does every checkable claim in an answer appear in what the run actually saw?

"Never fabricate a query result" is the hardest rule to see broken: a made-up row count reads exactly
like a real one, and no human checks an unattended run (OPEN_ITEMS.md #68). This module extracts the
claims that can be checked mechanically from a final answer and looks each one up in the run's
evidence: the prompt the agent was sent (pre-fetch included), every tool result, and the queries it
ran. A claim found nowhere in that evidence is *unsupported*: possibly fabricated, possibly derived
(a difference, a ratio) or remembered from an earlier turn. The engine sends unsupported claims back
to the agent once, to verify, label or remove (domain/engagement_policy.py).

Deliberately narrow, so a flag is worth a look:
- numbers with 4+ digits, comma-grouped numbers, decimals and percentages, never a bare year, a date,
  a time or a small count (short numbers match too much evidence by chance to prove anything);
- fully-qualified table names in the deployment's catalogs (`<catalog>.<schema>.<table>`; any
  `*_prod`/`*_dev` catalog when none are configured);
- file names of notebooks, scripts, TMDL files and Fabric items.
Fenced code blocks are skipped (SQL/DAX/mermaid the agent proposes is a proposal, not a claim), and
so is the scotri-learning block. Pure: no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\b")
_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_NUMBER = re.compile(r"(?<![\w.,/-])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d{4,})(%?)(?![\w/-])")
_YEAR = re.compile(r"^(19|20)\d{2}$")
_TABLE = re.compile(r"\b([A-Za-z0-9_]+_(?:prod|dev)\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+)\b")
"""Fallback when no catalogs are configured (config/client.yaml or the repo registry)."""


def table_pattern(catalogs: tuple[str, ...] | list[str] = ()) -> re.Pattern[str]:
    """Fully-qualified table names in `catalogs`, or the generic `*_prod`/`*_dev` fallback."""
    names = sorted({c for c in catalogs if c}, key=len, reverse=True)
    if not names:
        return _TABLE
    alternatives = "|".join(re.escape(c) for c in names)
    return re.compile(rf"\b((?:{alternatives})\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+)\b")
_FILE = re.compile(r"[\w\- ()]*?[\w\-()]+\.(?:ipynb|py|tmdl|Report|SemanticModel|pbir|pbism)\b")

MAX_CLAIMS = 15
TAG = "[Grounding check]"
"""Starts the engine's correction prompt; the transcript reader keeps a job's steps going across it
and never counts it as evidence (it lists the very claims being checked)."""


@dataclass(frozen=True)
class GroundingReport:
    checked: int
    unsupported: list[str]

    @property
    def ok(self) -> bool:
        return not self.unsupported


def _digits(value: str) -> str:
    return value.replace(",", "")


def _normalise_evidence(evidence: str) -> tuple[str, str]:
    """The evidence as-is (case-folded) and with digit-group commas removed, so 194,815 in an answer
    matches 194815 in a query result and vice versa."""
    folded = evidence.casefold()
    return folded, re.sub(r"(?<=\d),(?=\d{3}\b)", "", folded)


def extract_claims(answer: str, catalogs: tuple[str, ...] | list[str] = ()) -> list[str]:
    """The checkable claims in `answer`, in order of first appearance, without duplicates."""
    text = _FENCE.sub(" ", answer or "")
    text = _TIME.sub(" ", _DATE.sub(" ", text))
    seen: dict[str, None] = {}
    for m in table_pattern(catalogs).finditer(text):
        seen.setdefault(m.group(1), None)
    for m in _FILE.finditer(text):
        name = m.group(0).strip().strip("()")
        name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
        if name and "." in name:
            seen.setdefault(name, None)
    for m in _NUMBER.finditer(text):
        number, percent = m.group(1), m.group(2)
        if not percent and _YEAR.match(number):
            continue
        seen.setdefault(number + percent, None)
    return list(seen)


def _supported(claim: str, folded: str, nocommas: str) -> bool:
    c = claim.casefold()
    if c.endswith("%"):
        c = c[:-1]
    if re.fullmatch(r"[\d,.]+", c):
        bare = _digits(c)
        # A number is supported when it appears as a whole token, in either spelling.
        pattern = re.compile(r"(?<![\d.])" + re.escape(bare) + r"(?![\d])")
        return bool(pattern.search(nocommas)) or bool(re.search(r"(?<![\d.,])" + re.escape(c) + r"(?![\d])", folded))
    return c in folded


def check_grounding(answer: str, evidence: str, catalogs: tuple[str, ...] | list[str] = ()) -> GroundingReport:
    claims = extract_claims(answer, catalogs)
    folded, nocommas = _normalise_evidence(evidence or "")
    unsupported = [c for c in claims if not _supported(c, folded, nocommas)]
    return GroundingReport(checked=len(claims), unsupported=unsupported[:MAX_CLAIMS])


def correction_prompt(report: GroundingReport) -> str:
    """The follow-up the engine sends, resumed into the same session, when claims are unsupported."""
    listed = "\n".join(f"- `{c}`" for c in report.unsupported)
    return (
        f"{TAG} The engine compared your answer with every tool result, query and the prompt "
        f"in this run. These {len(report.unsupported)} value(s) appear in none of them:\n\n{listed}\n\n"
        "For each one: if it came from a source you didn't record, check it now with a governed read; "
        "if you derived it (a difference, ratio, total), say so in the text and name the inputs; if you "
        "can't support it, remove it or mark it as an assumption. Don't change anything else. Reply with "
        "the complete corrected final answer, which replaces your previous one, ending with the "
        "scotri-learning block as before."
    )


def unsupported_note(report: GroundingReport) -> str:
    """Appended to the answer when the correction turn couldn't run, so nothing passes silently."""
    listed = ", ".join(f"`{c}`" for c in report.unsupported)
    return ("\n\n---\n**Grounding check:** these values in the answer above don't appear in any tool "
            f"result or query from this run, so treat them as unverified: {listed}.")
