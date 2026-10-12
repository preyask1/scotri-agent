"""Team-wide learnings from past investigations (OPEN_ITEMS.md #58).

Decided 2026-10-01: knowledge from one investigation must be reusable by the next one on a similar
problem, whoever asks (a person or a webhook/service run). The unit shared is a *learning*, not the
full result: a result holds Databricks rows and DAX numbers queried as one shared service principal,
and sharing those team-wide would bypass the data-plane authorization the parked Phase-2 item exists
for. A learning carries the symptom, what was checked, the verdict, the root cause and the takeaways,
which is what the next investigation actually needs.

The agent ends its final message with a fenced ```scotri-learning``` JSON block. The engine (not the
agent) extracts it on a SUCCEEDED run, validates and redacts it, stores it, and strips it from the
user-visible result. A missing or malformed block stores nothing: fail closed, never a guessed record.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from domain.redaction import redact_secrets

BLOCK_TAG = "scotri-learning"
_BLOCK_RE = re.compile(r"```" + BLOCK_TAG + r"[ \t]*\r?\n(.*?)\r?\n```[ \t]*", re.DOTALL)

VERDICTS = {"confirmed_bug", "not_a_bug", "inconclusive", "story", "question"}
CONFIDENCE = {"high", "medium", "low"}

_MAX_TEXT = 1000

# "No data values in a learning" (agent-instructions.md) is enforced here, not just asked for: early
# learnings carried a sales total, duplicate counts per role and division, and table row counts,
# mostly in `checked`, where the agent noted what a query returned (OPEN_ITEMS.md #60(b)). A learning
# crosses users, so data-shaped figures become <n>. Numbers that say *where* or *what kind* survive:
# line/cell/version/id/attachment/job/run numbers, HTTP codes, durations, dates, times, years, and
# digits inside identifiers (Wave3, v19, PROJ-2118, table names).
_FIGURE = re.compile(r"(?<![\w.\-/])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d{3,})(%?)(?![\w\-/])"
                     r"|(?<![\w.\-/])(\d{1,2})(%)")
_KEEP_BEFORE = re.compile(
    r"\b(?:lines?|cells?|versions?|v|version\s+as\s+of|attachments?|ids?|jobs?|runs?|comments?|http|"
    r"status|code|port|steps?|stages?|priority|top|limit|cap|row\s+cap|issue|ticket)\s*[#:=]?\s*"
    r"(?:(?:\d+|<n>)\s*(?:[-,/]|and|to)\s*)*$", re.IGNORECASE)
_KEEP_AFTER = re.compile(r"^\s*(?:hours?|h|minutes?|mins?|seconds?|secs?|ms|days?|weeks?|months?|years?|"
                         r"mb|gb|kb|bytes?|chars?|characters?|tokens?)\b", re.IGNORECASE)
_HTTP_CODES = {"400", "401", "403", "404", "405", "409", "429", "500", "502", "503", "504"}
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_DATE_OR_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b|\b\d{1,2}:\d{2}(?::\d{2})?\b")


def scrub_figures(text: str) -> tuple[str, int]:
    """Replace data-shaped figures in `text` with <n>. Returns (text, how many were replaced)."""
    if not text:
        return text, 0
    spans = [m.span() for m in _DATE_OR_TIME.finditer(text)]
    out, last, count = [], 0, 0
    for m in _FIGURE.finditer(text):
        if any(a <= m.start() < b for a, b in spans):
            continue
        number, percent = m.group(1) or m.group(3), m.group(2) or m.group(4) or ""
        before, after = text[max(0, m.start() - 60):m.start()], text[m.end():m.end() + 20]
        if not percent and (_YEAR.match(number) or number in _HTTP_CODES
                            or _KEEP_BEFORE.search(before) or _KEEP_AFTER.match(after)):
            continue
        out.append(text[last:m.start()] + "<n>" + percent)
        last = m.end()
        count += 1
    return "".join(out) + text[last:], count
_MAX_ITEMS = 20
_MAX_ITEM_CHARS = 300


@dataclass(frozen=True)
class Learning:
    job_id: str
    title: str
    verdict: str
    takeaways: list[str]
    entities: list[str]
    created_at: datetime
    source_subject: str
    source_display: str
    ticket_key: str | None = None
    symptom: str = ""
    checked: list[str] = field(default_factory=list)
    root_cause_category: str | None = None
    root_cause: str = ""
    confidence: str | None = None

    def search_text(self) -> str:
        parts = [self.title, self.ticket_key or "", self.symptom, self.root_cause,
                 self.root_cause_category or "", self.verdict,
                 " ".join(self.checked), " ".join(self.takeaways), " ".join(self.entities)]
        return "\n".join(p for p in parts if p)


@dataclass(frozen=True)
class Extraction:
    cleaned_text: str
    fields: dict | None
    problem: str | None
    """Why no learning was recorded (missing/invalid block), for the log. None when one was found."""


def _str(v, limit=_MAX_TEXT, figures: bool = True) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError("expected a string")
    text = redact_secrets(v.strip())
    return (scrub_figures(text)[0] if figures else text)[:limit]


def _list(v, figures: bool = True) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError("expected a list of strings")
    out = [redact_secrets(x.strip()) for x in v if x.strip()]
    out = [(scrub_figures(x)[0] if figures else x)[:_MAX_ITEM_CHARS] for x in out]
    return out[:_MAX_ITEMS]


def _validate(raw: dict) -> dict:
    title = _str(raw.get("title"), 200)
    if not title:
        raise ValueError("'title' is required")
    verdict = _str(raw.get("verdict"), 40, figures=False).lower()
    if verdict not in VERDICTS:
        raise ValueError(f"'verdict' must be one of {sorted(VERDICTS)}")
    takeaways = _list(raw.get("takeaways"))
    if not takeaways:
        raise ValueError("'takeaways' must list at least one learning")
    confidence = _str(raw.get("confidence"), 10, figures=False).lower() or None
    if confidence is not None and confidence not in CONFIDENCE:
        raise ValueError(f"'confidence' must be one of {sorted(CONFIDENCE)}")
    return {
        "title": title,
        "verdict": verdict,
        "takeaways": takeaways,
        "entities": _list(raw.get("entities"), figures=False),  # names, not data
        "ticket_key": _str(raw.get("ticket_key"), 40, figures=False) or None,
        "symptom": _str(raw.get("symptom")),
        "checked": _list(raw.get("checked")),
        "root_cause_category": _str(raw.get("root_cause_category"), 40, figures=False) or None,
        "root_cause": _str(raw.get("root_cause")),
        "confidence": confidence,
    }


def extract_learning(text: str) -> Extraction:
    """Find the last scotri-learning block, strip every such block from the text, validate it."""
    matches = list(_BLOCK_RE.finditer(text or ""))
    if not matches:
        return Extraction(cleaned_text=text, fields=None, problem="no scotri-learning block")
    cleaned = _BLOCK_RE.sub("", text).rstrip()
    try:
        raw = json.loads(matches[-1].group(1))
        if not isinstance(raw, dict):
            raise ValueError("block is not a JSON object")
        return Extraction(cleaned_text=cleaned, fields=_validate(raw), problem=None)
    except (ValueError, json.JSONDecodeError) as e:
        return Extraction(cleaned_text=cleaned, fields=None, problem=f"invalid scotri-learning block: {e}")
