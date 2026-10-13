# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Markdown -> Atlassian Document Format (ADF) conversion for jira-write bodies.

Jira REST API v3 comment/description bodies are ADF, not plain text or markdown. Before this
module existed, `rest_writer_adapter.py` flattened every body into one plain paragraph, so any
markdown a skill drafted (headings, bullet lists, code blocks, bold/italic, links) came out on the
ticket as a single wall of unformatted text — the ticket reader lost the structure the drafting
skill (and the user who approved it) actually saw. This module preserves that structure by mapping
a small, deliberately-bounded markdown subset onto the matching ADF node types, so what gets
approved in chat is what actually renders on the ticket.

Supported block syntax: paragraphs, ATX headings (`#`..`######`), fenced code blocks (```lang),
bullet lists (`-`/`*`/`+`), ordered lists (`1.`/`1)`), and blockquotes (`>`). Supported inline
syntax: **bold**/__bold__, *italic*/_italic_, `inline code`, [link text](href), and `@[Name]`
mentions — mentions are resolved to a Jira accountId via the `mention_resolver` callback (see
`JiraRestTicketWriter.find_account_id`) and degrade to plain `@Name` text if no resolver is given
or the name doesn't resolve, rather than failing the whole conversion.
"""
from __future__ import annotations

import re
from typing import Callable

MentionResolver = Callable[[str], "str | None"]

_INLINE_PATTERN = re.compile(
    r"@\[(?P<mention_name>[^\]]+)\]"
    r"|\[(?P<link_text>[^\]]+)\]\((?P<link_href>[^)]+)\)"
    r"|`(?P<code_text>[^`]+)`"
    r"|\*\*(?P<bold_text>[^*]+?)\*\*"
    r"|__(?P<bold_text2>[^_]+?)__"
    r"|\*(?P<italic_text>[^*]+?)\*"
    r"|_(?P<italic_text2>[^_]+?)_"
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\d+[.)]\s+(.*)$")
_QUOTE_PREFIX_RE = re.compile(r"^>\s?")


def _inline_to_adf(text: str, mention_resolver: MentionResolver | None) -> list[dict]:
    nodes: list[dict] = []
    pos = 0
    for m in _INLINE_PATTERN.finditer(text):
        if m.start() > pos:
            plain = text[pos:m.start()]
            if plain:
                nodes.append({"type": "text", "text": plain})

        if m.group("mention_name") is not None:
            name = m.group("mention_name")
            account_id = mention_resolver(name) if mention_resolver is not None else None
            if account_id:
                nodes.append({"type": "mention", "attrs": {"id": account_id, "text": f"@{name}"}})
            else:
                nodes.append({"type": "text", "text": f"@{name}"})
        elif m.group("link_text") is not None:
            nodes.append({
                "type": "text",
                "text": m.group("link_text"),
                "marks": [{"type": "link", "attrs": {"href": m.group("link_href")}}],
            })
        elif m.group("code_text") is not None:
            nodes.append({"type": "text", "text": m.group("code_text"), "marks": [{"type": "code"}]})
        elif m.group("bold_text") is not None or m.group("bold_text2") is not None:
            nodes.append({
                "type": "text",
                "text": m.group("bold_text") or m.group("bold_text2"),
                "marks": [{"type": "strong"}],
            })
        elif m.group("italic_text") is not None or m.group("italic_text2") is not None:
            nodes.append({
                "type": "text",
                "text": m.group("italic_text") or m.group("italic_text2"),
                "marks": [{"type": "em"}],
            })
        pos = m.end()

    if pos < len(text):
        remainder = text[pos:]
        if remainder:
            nodes.append({"type": "text", "text": remainder})

    if not nodes:
        nodes.append({"type": "text", "text": text or " "})
    return nodes


def _list_block(kind: str, items: list[str], mention_resolver: MentionResolver | None) -> dict:
    return {
        "type": kind,
        "content": [
            {
                "type": "listItem",
                "content": [{"type": "paragraph", "content": _inline_to_adf(item, mention_resolver)}],
            }
            for item in items
        ],
    }


def markdown_to_adf(text: str, mention_resolver: MentionResolver | None = None) -> dict:
    """Convert a markdown-subset string into an ADF `doc` node ready to POST/PUT as a Jira
    comment or description body."""
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: list[dict] = []
    paragraph_buf: list[str] = []
    i = 0

    def flush_paragraph() -> None:
        if paragraph_buf:
            blocks.append({
                "type": "paragraph",
                "content": _inline_to_adf(" ".join(paragraph_buf), mention_resolver),
            })
            paragraph_buf.clear()

    while i < len(lines):
        stripped = lines[i].strip()

        if stripped.startswith("```"):
            flush_paragraph()
            lang = stripped[3:].strip() or None
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip the closing fence (or EOF if the fence was never closed)
            node: dict = {"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(code_lines)}]}
            if lang:
                node["attrs"] = {"language": lang}
            blocks.append(node)
            continue

        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            flush_paragraph()
            blocks.append({
                "type": "heading",
                "attrs": {"level": len(heading_match.group(1))},
                "content": _inline_to_adf(heading_match.group(2), mention_resolver),
            })
            i += 1
            continue

        if _BULLET_RE.match(stripped):
            flush_paragraph()
            items = []
            while i < len(lines) and _BULLET_RE.match(lines[i].strip()):
                items.append(_BULLET_RE.match(lines[i].strip()).group(1))
                i += 1
            blocks.append(_list_block("bulletList", items, mention_resolver))
            continue

        if _ORDERED_RE.match(stripped):
            flush_paragraph()
            items = []
            while i < len(lines) and _ORDERED_RE.match(lines[i].strip()):
                items.append(_ORDERED_RE.match(lines[i].strip()).group(1))
                i += 1
            blocks.append(_list_block("orderedList", items, mention_resolver))
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(_QUOTE_PREFIX_RE.sub("", lines[i].strip()))
                i += 1
            blocks.append({
                "type": "blockquote",
                "content": [{"type": "paragraph", "content": _inline_to_adf(" ".join(quote_lines), mention_resolver)}],
            })
            continue

        if not stripped:
            flush_paragraph()
            i += 1
            continue

        paragraph_buf.append(stripped)
        i += 1

    flush_paragraph()

    if not blocks:
        blocks = [{"type": "paragraph", "content": [{"type": "text", "text": ""}]}]

    return {"type": "doc", "version": 1, "content": blocks}
