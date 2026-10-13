"""Lexical retrieval (BM25) — pure, corpus-agnostic ranking (OPEN_ITEMS.md item #38).

The hard constraint from the design note (docs/ai/retrieval-and-graph-design.md §2), enforced by how
this is *used*, not by this module: retrieval returns *similar* content, never *complete* content, so
it must only ever RANK an already-complete candidate set — never be the candidate generator for
impact-analysis, where a missed fifth consumer of a gold table is a confident, well-formed, wrong
answer. This module ranks; the always-trace-all-four-repos rule still owns completeness.

BM25 only, no embeddings — a deliberate first step (OPEN_ITEMS.md #38, confirmed 2026-09-29 given the
Azure/Foundry direction): no new credential, no outbound call, nothing against the one-portable-folder
premise. A hybrid embeddings adapter (Foundry-hosted, in-tenant) can be added later behind the same
retrieval port. Lexical also handles the exact-identifier tokens (USP_*, NB_*, <prod_catalog>.gold.*) that
pure vector search does badly — which is why the eventual design is hybrid, not vector-only.

Pure domain: no file IO, no sqlite. The adapter assembles Documents from its corpora and calls rank().
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

_K1 = 1.5
_B = 0.75
# Identifier-aware: [a-z0-9_.]+ keeps USP_SALES_TREND and <prod_catalog>.gold.visit_summary as whole tokens.
_TOKEN_RE = re.compile(r"[a-z0-9_.]+")


def tokenize(text: str) -> list[str]:
    """Whole identifiers PLUS their `.`/`_`-split pieces, so both an exact `<prod_catalog>.gold.visit_summary`
    match and a looser `visit`/`summary` match contribute — the lexical half of what would otherwise
    need embeddings to catch."""
    tokens: list[str] = []
    for whole in _TOKEN_RE.findall(text.lower()):
        tokens.append(whole)
        parts = [p for p in re.split(r"[._]", whole) if p and p != whole]
        tokens.extend(parts)
    return tokens


@dataclass(frozen=True)
class Document:
    id: str
    scope: str  # "past-engagements" | "docs" | ...
    title: str
    text: str
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalHit:
    id: str
    scope: str
    title: str
    score: float
    snippet: str
    meta: dict = field(default_factory=dict)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_SPLIT_LEVEL = 2          # every `#`/`##` heading starts a section
_SUBSPLIT_CHARS = 8000    # a section still larger than this is split again on `###`


@dataclass(frozen=True)
class Section:
    heading: str      # breadcrumb of enclosing headings, "" for text before the first heading
    start_line: int   # 1-based, inclusive
    end_line: int     # 1-based, inclusive
    text: str


def split_markdown(text: str) -> list[Section]:
    """Split a markdown document into heading-delimited sections, so a retrieval hit points at the
    part that matched (and its line range) instead of a whole 30 KB file. Splits on `#`/`##`, then
    re-splits any section over `_SUBSPLIT_CHARS` on `###`. Headings inside fenced code are ignored.
    A document with no headings comes back as one section."""
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []  # (0-based line index, level, title)
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and (m := _HEADING_RE.match(line)):
            headings.append((i, len(m.group(1)), m.group(2).strip()))

    def cut(level: int, lo: int, hi: int) -> list[tuple[int, int]]:
        """[lo, hi) cut at every heading of `level` or above strictly inside it."""
        bounds = [lo, *(i for i, lvl, _ in headings if lvl <= level and lo < i < hi), hi]
        return list(zip(bounds, bounds[1:]))

    spans: list[tuple[int, int]] = []
    for a, b in cut(_SPLIT_LEVEL, 0, len(lines)):
        if sum(len(line) + 1 for line in lines[a:b]) > _SUBSPLIT_CHARS:
            spans.extend(cut(_SPLIT_LEVEL + 1, a, b))
        else:
            spans.append((a, b))

    sections: list[Section] = []
    for a, b in spans:
        body = "\n".join(lines[a:b]).strip()
        if not body:
            continue
        trail: list[tuple[int, str]] = []  # (level, title) of headings enclosing line a
        for i, lvl, title in headings:
            if i > a:
                break
            trail = [t for t in trail if t[0] < lvl] + [(lvl, title)]
        sections.append(Section(" › ".join(t for _, t in trail), a + 1, b, body))
    return sections


def _snippet(text: str, query_tokens: set[str], width: int = 240) -> str:
    """A window around the first query-term hit, so the snippet shows *why* the doc matched rather
    than always the document's opening."""
    lowered = text.lower()
    best = -1
    for qt in query_tokens:
        idx = lowered.find(qt)
        if idx != -1 and (best == -1 or idx < best):
            best = idx
    if best == -1:
        return text[:width].strip()
    start = max(0, best - width // 3)
    return ("…" if start > 0 else "") + text[start:start + width].strip() + ("…" if start + width < len(text) else "")


def rank(query: str, documents: list[Document], limit: int) -> list[RetrievalHit]:
    """BM25 rank `documents` against `query`, returning the top `limit` with a score > 0.

    A query term absent from the whole corpus contributes nothing (idf skips it); a document that
    matches no query term is dropped rather than returned with score 0, so a caller never mistakes
    "here are some matches" for "here is everything"."""
    if not documents:
        return []
    q_tokens = tokenize(query)
    q_set = set(q_tokens)
    if not q_set:
        return []

    doc_tokens = [tokenize(d.text + " " + d.title) for d in documents]
    doc_len = [len(t) for t in doc_tokens]
    avgdl = (sum(doc_len) / len(doc_len)) or 1.0
    n = len(documents)

    # Document frequency per query term.
    df: Counter[str] = Counter()
    for toks in doc_tokens:
        present = set(toks)
        for qt in q_set:
            if qt in present:
                df[qt] += 1

    hits: list[RetrievalHit] = []
    for i, d in enumerate(documents):
        tf = Counter(doc_tokens[i])
        score = 0.0
        for qt in q_set:
            if df[qt] == 0:
                continue
            idf = math.log(1 + (n - df[qt] + 0.5) / (df[qt] + 0.5))
            freq = tf.get(qt, 0)
            if freq == 0:
                continue
            denom = freq + _K1 * (1 - _B + _B * doc_len[i] / avgdl)
            score += idf * (freq * (_K1 + 1)) / denom
        if score > 0:
            hits.append(RetrievalHit(
                id=d.id, scope=d.scope, title=d.title, score=round(score, 4),
                snippet=_snippet(d.text, q_set), meta=d.meta,
            ))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]
