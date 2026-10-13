# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Who writes and who reads a table, from code in the mirror (table-usage, OPEN_ITEMS.md #68). Pure
functions: no filesystem, no network. The CLI hands in each file's text view (notebooks rendered as
cells, so line numbers match `mirror-read --as-text --lines`).

Why this exists: "which notebook builds this table, and what reads it" is the first question of most
investigations. On PROJ-2118 it took four mirror-grep calls and a whole-notebook read. One pass here
classifies every line that names the table as a write, a read or a mention, and groups the hits by
file with the notebook cell they sit in, so the agent reads only the lines that matter.

Matching is on the table's last name segment, with word boundaries, because code names tables many
ways (`<prod_catalog>.gold.x`, `gold.x`, f"{catalog}.gold.x", saveAsTable("x")). The classification is a
heuristic on the matching line: treat it as where to look, not as proof.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_WRITE = re.compile(
    r"saveAsTable|insertInto|\bINSERT\s+(?:INTO|OVERWRITE)\b|\bCREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(?:TEMP(?:ORARY)?\s+)?(?:TABLE|VIEW)\b|\bMERGE\s+INTO\b|\bDELETE\s+FROM\b|\bTRUNCATE\s+TABLE\b"
    r"|\bUPDATE\s+[\w.`{}$]+\s+SET\b|\.write\b.*\.table\s*\(|\bREPLACE\s+TABLE\b", re.IGNORECASE)
_READ = re.compile(r"\bFROM\b|\bJOIN\b|spark\.table\s*\(|read\.table\s*\(|\.table\s*\(|\bUSING\b",
                   re.IGNORECASE)
_CELL = re.compile(r"^# ── cell (\d+) \[")
_COMMENT = re.compile(r"^\s*(?:#|--|//)")
_MAX_LINE = 240


@dataclass
class FileUsage:
    path: str
    kind: str
    roles: set[str] = field(default_factory=set)
    lines: list[dict] = field(default_factory=list)


def table_pattern(table: str) -> re.Pattern:
    """Matches the table's last name segment as a whole identifier."""
    name = table.strip().strip("`").split(".")[-1]
    if not re.fullmatch(r"\w+", name or ""):
        raise ValueError(f"not a table name: {table!r}")
    return re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])", re.IGNORECASE)


def file_kind(path: str) -> str | None:
    """What a file is for table-usage, or None when it isn't scanned. Report definitions are left
    out: their query JSON says "From" for every field, and report-bindings already answers which
    reports use a table."""
    p = path.lower()
    if ".report/" in p:
        return None
    if p.endswith((".ipynb", ".py", ".sql")):
        return "notebook"
    if ".semanticmodel/" in p and p.endswith((".tmdl", ".bim", ".json")):
        return "semantic-model"
    if p.endswith(".json") and any(f"{d}/" in p for d in ("pipeline", "dataset", "dataflow", "trigger")):
        return "adf"
    return None


def classify(line: str) -> str:
    if _COMMENT.match(line):
        return "mention"
    if _WRITE.search(line):
        return "write"
    if _READ.search(line):
        return "read"
    return "mention"


def scan_text(path: str, text: str, pattern: re.Pattern, max_lines: int = 12) -> FileUsage | None:
    """Every line of one file that names the table, classified, with its notebook cell. None when the
    file never names it."""
    usage = None
    cell = None
    for number, line in enumerate(text.splitlines(), 1):
        m = _CELL.match(line)
        if m:
            cell = int(m.group(1))
            continue
        if not pattern.search(line):
            continue
        if usage is None:
            usage = FileUsage(path=path, kind=file_kind(path) or "other")
        role = classify(line)
        if usage.kind == "semantic-model" and role == "mention" and not _COMMENT.match(line):
            role = "read"  # a model naming the table is a model loading it (M partition / native query)
        usage.roles.add(role)
        if len(usage.lines) < max_lines:
            entry = {"line": number, "role": role, "text": line.strip()[:_MAX_LINE]}
            if cell is not None:
                entry["cell"] = cell
            usage.lines.append(entry)
    return usage


_ROLE_ORDER = {"write": 0, "read": 1, "mention": 2}


def _model_folder(path: str) -> str | None:
    m = re.match(r"(.*?\.SemanticModel)/", path, re.IGNORECASE)
    return m.group(1) if m else None


def _merge_models(usages: list[tuple[str, str, FileUsage]], max_lines: int) -> list[tuple[str, str, FileUsage]]:
    """One entry per semantic model rather than one per TMDL file (model, relationships, roles,
    cultures...): each line keeps the file it came from."""
    merged: dict[tuple[str, str, str], FileUsage] = {}
    out = []
    for repo, branch, u in usages:
        folder = _model_folder(u.path) if u.kind == "semantic-model" else None
        if folder is None:
            out.append((repo, branch, u))
            continue
        key = (repo, branch, folder)
        if key not in merged:
            merged[key] = FileUsage(path=folder, kind=u.kind)
            out.append((repo, branch, merged[key]))
        target = merged[key]
        target.roles |= u.roles
        for line in u.lines:
            if len(target.lines) < max_lines:
                target.lines.append({"file": u.path[len(folder) + 1:], **line})
    return out


def summarize(usages: list[tuple[str, str, FileUsage]], max_lines: int = 12) -> dict:
    """Group (repo, branch, usage) into writers / readers / mentions, writers first, one entry per
    semantic model."""
    usages = _merge_models(usages, max_lines)
    out = {"writers": [], "readers": [], "mentions": []}
    for repo, branch, u in sorted(usages, key=lambda t: (min(_ROLE_ORDER[r] for r in t[2].roles), t[2].path)):
        item = {"repo": repo, "branch": branch, "path": u.path, "kind": u.kind,
                "roles": sorted(u.roles, key=_ROLE_ORDER.get), "lines": u.lines}
        bucket = "writers" if "write" in u.roles else "readers" if "read" in u.roles else "mentions"
        out[bucket].append(item)
    return out
