"""Plain-text views of mirror files, and the search over them (mirror-read --as-text, mirror-find,
mirror-grep). Pure functions: no filesystem, no network. The adapter hands in paths and contents.

Why this exists: a notebook read from the mirror arrives as .ipynb JSON. On PROJ-2085 the agent spent
several calls parsing it with ad-hoc Python and re-read the same notebook five times when a /tmp path
meant different things to Git Bash and to Windows Python. Rendering cells as text here gives every
caller the same view, with the same line numbers that mirror-grep reports.
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass

MANIFEST_NAME = ".scotri-manifest.json"
"""File list the sync service writes into each mirrored tree before the swap, so it is swapped in
atomically with the tree it describes. Never shown by mirror-list/-find, never matched by -grep."""

_SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".pbix", ".pbit", ".zip", ".gz",
    ".parquet", ".xlsx", ".xls", ".pdf", ".dll", ".exe", ".bin", ".ttf", ".woff", ".woff2",
)
MAX_GREP_FILE_BYTES = 5 * 1024 * 1024
_MAX_LINE_CHARS = 300


def notebook_to_text(raw: str) -> str:
    """Render an .ipynb as its cells' source, each under a `# ── cell N [type] ──` header. Outputs
    are dropped. Anything that isn't notebook JSON comes back unchanged, so a caller can pass any
    file through without checking its type first."""
    try:
        nb = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        return raw
    parts = []
    for index, cell in enumerate(nb["cells"]):
        if not isinstance(cell, dict):
            continue
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(str(s) for s in source)
        parts.append(f"# ── cell {index} [{cell.get('cell_type', '?')}] ──\n{str(source).rstrip()}\n")
    return "\n".join(parts)


def as_text(path: str, raw: str) -> str:
    return notebook_to_text(raw) if path.lower().endswith(".ipynb") else raw


def parse_line_range(spec: str) -> tuple[int, int]:
    """'120-200' -> (120, 200); '120' -> (120, 120). 1-based, inclusive."""
    match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", spec or "")
    if not match:
        raise ValueError(f"--lines must look like 120-200 or 120, got {spec!r}")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start < 1 or end < start:
        raise ValueError(f"--lines range {spec!r} is empty or starts before line 1")
    return start, end


def slice_lines(text: str, start: int, end: int) -> tuple[str, int]:
    """Lines start..end (1-based, inclusive) and the total line count."""
    lines = text.splitlines()
    return "\n".join(lines[start - 1:end]), len(lines)


def is_searchable(path: str) -> bool:
    lowered = path.lower()
    return not lowered.endswith(_SKIP_SUFFIXES) and not lowered.endswith(MANIFEST_NAME)


def find_paths(paths: list[str], pattern: str, limit: int) -> tuple[list[str], bool]:
    """Case-insensitive match on the repo-relative path. A pattern with * ? or [ is a glob, matched
    against both the full path and the file name; anything else is a substring. Returns the first
    `limit` matches and whether more existed."""
    needle = pattern.lower()
    is_glob = any(c in needle for c in "*?[")
    matches = []
    for path in paths:
        lowered = path.lower()
        if is_glob:
            hit = fnmatch.fnmatchcase(lowered, needle) or fnmatch.fnmatchcase(lowered.rsplit("/", 1)[-1], needle)
        else:
            hit = needle in lowered
        if hit:
            if len(matches) == limit:
                return matches, True
            matches.append(path)
    return matches, False


@dataclass(frozen=True)
class GrepHit:
    path: str
    line: int
    text: str


def grep_text(path: str, text: str, regex: re.Pattern, remaining: int) -> list[GrepHit]:
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        if regex.search(line):
            hits.append(GrepHit(path, number, line.strip()[:_MAX_LINE_CHARS]))
            if len(hits) >= remaining:
                break
    return hits
