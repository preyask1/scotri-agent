# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Diagrams carried in a run's answer, lifted out as structured data for API/MCP callers.

The agent writes a diagram as a fenced ```mermaid block in its final message (the `er-diagram`
skill does this for ER diagrams; any skill may). The answer text is unchanged, so a markdown
renderer that understands mermaid fences (scotri-ui does) draws it in place. This module also hands
callers that don't render markdown the same blocks as a list, so they can draw them with their own
mermaid or show the source, without parsing the answer themselves.

Pure: derived from the stored result on every read, nothing new is stored. The source is passed
through as written; mermaid's own `securityLevel: "strict"` on the rendering side is what keeps a
diagram from running script, not anything here.
"""
from __future__ import annotations

import re

MAX_DIAGRAMS = 10
# Mermaid's own default maxTextSize; a larger source would only fail to render on the client.
MAX_SOURCE_CHARS = 50_000

# A fence of 3+ backticks or tildes, info string starting "mermaid", closed by the same fence
# character, at least as long, on its own line (CommonMark's rule, which react-markdown follows).
_FENCE = re.compile(
    r"^[ ]{0,3}(?P<fence>`{3,}|~{3,})[ \t]*mermaid\b[^\n]*\n"
    r"(?P<body>.*?)"
    r"^[ ]{0,3}(?P=fence)[`~]*[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_FRONTMATTER_TITLE = re.compile(r"\A\s*---\s*\n(?P<fm>.*?)\n\s*---\s*\n", re.DOTALL)
_TITLE_LINE = re.compile(r"^\s*title\s*:\s*(?P<title>.+?)\s*$", re.MULTILINE)
_COMMENT = re.compile(r"^\s*%%.*$", re.MULTILINE)


def _kind(source: str) -> str:
    """The diagram type keyword (erDiagram, flowchart, ...), after any frontmatter and comments."""
    body = _FRONTMATTER_TITLE.sub("", source, count=1)
    body = _COMMENT.sub("", body)
    for line in body.splitlines():
        word = line.strip().split(maxsplit=1)
        if word:
            return word[0]
    return "unknown"


def _title(source: str) -> str | None:
    fm = _FRONTMATTER_TITLE.match(source)
    if not fm:
        return None
    m = _TITLE_LINE.search(fm.group("fm"))
    return m.group("title").strip("'\"") if m else None


def extract_diagrams(text: str | None) -> list[dict]:
    """Every mermaid fence in `text`, in order: [{"format", "kind", "title", "source"}].

    Capped at MAX_DIAGRAMS; a source over MAX_SOURCE_CHARS is skipped rather than truncated, since a
    cut-off diagram would render as an error or, worse, as a silently partial picture.
    """
    if not text:
        return []
    diagrams: list[dict] = []
    for m in _FENCE.finditer(text):
        source = m.group("body").rstrip("\n")
        if not source.strip() or len(source) > MAX_SOURCE_CHARS:
            continue
        diagrams.append({
            "format": "mermaid",
            "kind": _kind(source),
            "title": _title(source),
            "source": source,
        })
        if len(diagrams) >= MAX_DIAGRAMS:
            break
    return diagrams


# --- Lint: catch a diagram that won't render before it reaches anyone --------------------------------
#
# The agent can't run mermaid, and one bad line makes mermaid reject the whole diagram (the reader
# then sees an error and the source instead of a picture). This checks erDiagram blocks against the
# subset of the grammar the er-diagram skill writes: symbol cardinalities only, ASCII entity names,
# quoted labels. Stricter than mermaid in places (it accepts word cardinalities and unicode names);
# never looser, so a block that passes here renders.

_ENTITY = r"[A-Za-z_][A-Za-z0-9_-]*"
_LEFT = r"\|o|\|\||\}o|\}\|"
_RIGHT = r"o\||\|\||o\{|\|\{"
_REL_SHAPE = re.compile(rf"^(?P<a>\S+)\s+(?P<card>\S+)\s+(?P<b>\S+)\s*:\s*(?P<label>.*)$")
_CARD = re.compile(rf"^(?P<left>{_LEFT})(?P<line>--|\.\.)(?P<right>{_RIGHT})$")
_ENTITY_OPEN = re.compile(rf'^(?P<name>{_ENTITY})(\["[^"]*"\])?\s*\{{\s*$')
_ENTITY_ALONE = re.compile(rf"^{_ENTITY}$")
_ATTR_WORD = r"[A-Za-z_][A-Za-z0-9_\-\[\]()]*"
_ATTRIBUTE = re.compile(
    rf'^{_ATTR_WORD}\s+\*?{_ATTR_WORD}(\s+(PK|FK|UK)(\s*,\s*(PK|FK|UK))*)?(\s+"[^"]*")?$'
)
_LABEL = re.compile(r'^("[^"]*"|[A-Za-z0-9_-]+)$')
_DIRECTION = re.compile(r"^direction\s+(TB|BT|LR|RL)$")
MAX_LABEL_CHARS = 120


def _fences_with_lines(text: str):
    """(1-based file line of the block's first source line, source) for each mermaid fence."""
    for m in _FENCE.finditer(text):
        first_line = text.count("\n", 0, m.start("body")) + 1
        yield first_line, m.group("body").rstrip("\n")


def _lint_er(source: str) -> tuple[list[tuple[int, str, str]], list[tuple[int, str, str]]]:
    """(errors, warnings) as (line within source, text, problem)."""
    errors: list[tuple[int, str, str]] = []
    warnings: list[tuple[int, str, str]] = []
    lines = source.splitlines()
    i = 0
    if lines and lines[0].strip() == "---":  # frontmatter
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            i += 1
        if i >= len(lines):
            return [(1, lines[0], "frontmatter '---' is never closed")], []
        i += 1
    declared: set[str] = set()
    referenced: list[tuple[int, str, str]] = []
    in_entity: tuple[int, str] | None = None
    seen_header = False
    for n in range(i, len(lines)):
        raw = lines[n]
        line = raw.strip()
        lineno = n + 1
        if not line or line.startswith("%%"):
            continue
        if not seen_header:
            if line != "erDiagram":
                errors.append((lineno, raw, "the first line after frontmatter must be exactly 'erDiagram'"))
            seen_header = True
            continue
        if in_entity:
            if line == "}":
                in_entity = None
            elif not _ATTRIBUTE.match(line):
                errors.append((lineno, raw, "not a valid attribute: expected 'type name [PK|FK|UK] [\"comment\"]' "
                                            "(single-word type and name, comment in double quotes with no \" inside)"))
            continue
        if m := _ENTITY_OPEN.match(line):
            in_entity = (lineno, m.group("name"))
            declared.add(m.group("name"))
            continue
        if _ENTITY_ALONE.match(line):
            declared.add(line)
            continue
        if _DIRECTION.match(line):
            continue
        if m := _REL_SHAPE.match(line):
            a, card, b, label = m.group("a"), m.group("card"), m.group("b"), m.group("label").strip()
            problems = []
            for name in (a, b):
                if not re.fullmatch(_ENTITY, name):
                    problems.append(f"entity name '{name}' must be letters, digits, _ or - only")
            if not _CARD.match(card):
                problems.append(
                    f"cardinality '{card}' is invalid: it needs a left marker (|o, ||, }}o, }}|), a line (-- or ..) "
                    "and a right marker (o|, ||, o{, |{), e.g. '}o..||'")
            if not _LABEL.match(label):
                problems.append("the label after ':' must be one word or a double-quoted string with no \" inside")
            if problems:
                errors.append((lineno, raw, "; ".join(problems)))
            else:
                referenced.extend([(lineno, raw, a), (lineno, raw, b)])
                if len(label) > MAX_LABEL_CHARS:
                    warnings.append((lineno, raw, f"label is {len(label)} characters; keep labels short "
                                                  "(the join columns) and put explanations under the diagram"))
            continue
        errors.append((lineno, raw, "not an entity block, an attribute, a relationship or a %% comment"))
    if not seen_header:
        errors.append((len(lines), "", "no 'erDiagram' line"))
    if in_entity:
        errors.append((in_entity[0], lines[in_entity[0] - 1], f"entity '{in_entity[1]}' is opened with '{{' but never closed"))
    for lineno, raw, name in referenced:
        if name not in declared:
            warnings.append((lineno, raw, f"'{name}' has no entity block; mermaid will draw it as an empty "
                                          "box (a typo, or a table you meant to describe?)"))
    return errors, warnings


def lint_diagrams(text: str) -> list[dict]:
    """Lint every mermaid fence in `text`. Line numbers are lines of `text` (the file), not the block.

    erDiagram blocks get the full check; other kinds are reported as not linted rather than passed.
    A block over MAX_SOURCE_CHARS is an error, since extract_diagrams would drop it.
    """
    results = []
    for index, (first_line, source) in enumerate(_fences_with_lines(text)):
        kind = _kind(source)
        # A misspelt header ("erDiagramX", "erdiagram") is still meant as an ER diagram: lint it so the
        # header error is reported, instead of passing it as "some other kind".
        is_er = kind.lower().startswith("erdiagram")
        result = {"index": index, "kind": kind, "title": _title(source), "start_line": first_line,
                  "linted": is_er, "errors": [], "warnings": []}
        if len(source) > MAX_SOURCE_CHARS:
            result["errors"].append({"line": first_line, "text": "",
                                     "problem": f"block is {len(source)} characters; the limit is "
                                                f"{MAX_SOURCE_CHARS}, so it would be dropped. Split it."})
        if is_er:
            errors, warnings = _lint_er(source)
            for key, found in (("errors", errors), ("warnings", warnings)):
                result[key].extend({"line": first_line + ln - 1, "text": t.strip()[:200], "problem": p}
                                   for ln, t, p in found)
        if index >= MAX_DIAGRAMS:
            result["errors"].append({"line": first_line, "text": "",
                                     "problem": f"more than {MAX_DIAGRAMS} diagrams; this one would be dropped"})
        results.append(result)
    return results
