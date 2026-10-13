# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""KPI definitions from the report documents (docs/ai/client/reports/<set>/*.md) attached to lineage
nodes. Pure: the caller hands in document text and the set of node ids in the graph.

The report docs say what a KPI *means* (business definition, caveats); the mirror says how it is
*computed* (DAX) and what feeds it. Joining the two on the measure node lets one lookup answer
both. The docs are a dated snapshot, so every definition attached here is `verified: False` and
carries the doc path and line it came from: the mirror stays the authority on what is deployed.

Section 2 of each doc ("Business Requirements & KPI Definitions") holds the KPI tables, in several
layouts (`KPI | Definition | Source Column / Measure | Notes`, `KPI | Measure | Description`,
`KPI | Source column | Meaning`, ...). A table counts when it has a definition-like column; a row
attaches to a measure when a whole backticked token, or the KPI name itself, is exactly a measure of
that model, else to columns whose name is exactly a backticked token. Names inside a formula never
count: `DATEDIFF([VISITDATE], ...)` must not pin "Avg Visit Report Days" to the VISITDATE column.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_MODEL_RE = re.compile(r"\*\*Semantic model:\*\*\s*`([^`]+)`")
_SECTION_RE = re.compile(r"^##\s+2\.")
_DEF_HEADERS = ("definition", "meaning", "description")
_NOTE_HEADERS = ("notes", "note")
_TOKEN_RE = re.compile(r"`([^`]+)`")


@dataclass
class KpiRow:
    name: str
    definition: str
    notes: str
    tokens: list[str]  # backticked tokens from every cell but the definition, in reading order
    line: int          # 1-based line in the doc


@dataclass
class KpiNote:
    node_id: str
    kpi: str
    definition: str
    source_ref: str  # "<doc path>:<line>"


@dataclass
class KpiAttachResult:
    notes: list[KpiNote] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)  # {"doc", "line", "kpi", "reason"}


def model_path(text: str) -> str | None:
    """The doc's semantic model path without `.SemanticModel`, forward slashes."""
    m = _MODEL_RE.search(text)
    if not m:
        return None
    path = m.group(1).replace("\\", "/").strip().rstrip("/")
    return path[:-len(".SemanticModel")] if path.endswith(".SemanticModel") else path


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def kpi_rows(text: str) -> list[KpiRow]:
    lines = text.splitlines()
    rows: list[KpiRow] = []
    in_section = False
    header: list[str] | None = None
    for i, line in enumerate(lines):
        if line.startswith("## "):
            in_section = bool(_SECTION_RE.match(line))
            header = None
            continue
        if not in_section:
            continue
        if not line.lstrip().startswith("|"):
            header = None
            continue
        cells = _cells(line)
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue  # the |---| separator
        di = next((k for k, h in enumerate(header) if any(d in h for d in _DEF_HEADERS)), None)
        if di is None or di >= len(cells) or di == 0:
            continue  # page list, business questions: no definitions in this table
        ni = next((k for k, h in enumerate(header) if h in _NOTE_HEADERS), None)
        tokens = [t for k, c in enumerate(cells) if k != di for t in _TOKEN_RE.findall(c)]
        rows.append(KpiRow(
            name=cells[0].replace("`", "").strip(), definition=cells[di],
            notes=cells[ni] if ni is not None and ni < len(cells) else "",
            tokens=tokens, line=i + 1))
    return rows


def _node_name(node_id: str) -> tuple[str, str]:
    """(table, name) for `column:<base>::<table>[<name>]`, ("", name) for `measure:<base>::[<name>]`."""
    rest = node_id.split("::", 1)[1]
    table, _, name = rest.rpartition("[")
    return table, name[:-1]


def _resolve_base(base: str, node_ids: set[str]) -> str:
    """The model node base the doc means: exact, else the one model whose path differs only by
    trailing `_`/spaces or case (a doc naming `X (Work Habit)` for the repo's `X (Work Habit)_`)."""
    if f"fabric_model:{base}" in node_ids:
        return base
    want = base.rstrip("_ ").lower()
    near = [n[len("fabric_model:"):] for n in node_ids
            if n.startswith("fabric_model:") and n[len("fabric_model:"):].rstrip("_ ").lower() == want]
    return near[0] if len(near) == 1 else base


def attach(doc_path: str, text: str, tree: str, node_ids: set[str]) -> KpiAttachResult:
    """Attach one doc's KPI rows to measure/column nodes of its model in `tree`."""
    out = KpiAttachResult()
    path = model_path(text)
    if path is None:
        return out
    base = _resolve_base(f"{tree}/{path}", node_ids)
    measures ={_node_name(n)[1].lower(): n for n in node_ids if n.startswith(f"measure:{base}::[")}
    columns: dict[str, list[str]] = {}
    for n in node_ids:
        if n.startswith(f"column:{base}::"):
            table, name = _node_name(n)
            columns.setdefault(name.lower(), []).append(n)
            columns.setdefault(f"{table}.{name}".lower(), []).append(n)
            columns.setdefault(f"{table}[{name}]".lower(), []).append(n)
    if not measures and not columns:
        rows = kpi_rows(text)
        if rows:
            out.unmatched.append({"doc": doc_path, "line": rows[0].line, "kpi": "*",
                                  "reason": f"model {base} has no measure/column nodes in the graph"})
        return out
    for row in kpi_rows(text):
        ref = f"{doc_path}:{row.line}"
        definition = row.definition + (f" (Note: {row.notes})" if row.notes else "")
        candidates = [t.strip() for t in row.tokens] + [row.name]
        hit = next((measures[c.lower()] for c in candidates if c.lower() in measures), None)
        targets = [hit] if hit else sorted({n for t in row.tokens for n in columns.get(t.strip().lower(), [])})
        if not targets:
            out.unmatched.append({"doc": doc_path, "line": row.line, "kpi": row.name,
                                  "reason": "no measure or column of that name in the model"})
            continue
        out.notes.extend(KpiNote(n, row.name, definition, ref) for n in targets)
    return out
