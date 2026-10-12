"""domain/diagrams.py: mermaid fences in a run's answer, lifted out for API/MCP callers."""
from domain.diagrams import MAX_DIAGRAMS, MAX_SOURCE_CHARS, extract_diagrams

ER = """---
title: Sales model
---
erDiagram
    FactSales }o--|| DimProduct : "ProductKey"
"""


def _fenced(body: str, fence: str = "```", info: str = "mermaid") -> str:
    return f"{fence}{info}\n{body}\n{fence}\n"


def test_no_text_or_no_fence_gives_nothing():
    assert extract_diagrams(None) == []
    assert extract_diagrams("") == []
    assert extract_diagrams("plain answer\n```sql\nSELECT 1\n```\n") == []


def test_er_diagram_with_frontmatter_title():
    [d] = extract_diagrams("Here it is:\n\n" + _fenced(ER.rstrip("\n")) + "\nDone.")
    assert d == {"format": "mermaid", "kind": "erDiagram", "title": "Sales model",
                 "source": ER.rstrip("\n")}


def test_kind_skips_comments_and_title_is_optional():
    [d] = extract_diagrams(_fenced("%% inferred edges are dashed\nflowchart LR\n  a --> b"))
    assert d["kind"] == "flowchart"
    assert d["title"] is None


def test_several_blocks_in_order_tilde_and_longer_fences():
    text = (_fenced("erDiagram\n  A ||--o{ B : x")
            + "between\n"
            + _fenced("flowchart TD\n  x --> y", fence="~~~~")
            + _fenced("sequenceDiagram\n  a->>b: hi\n```", fence="````"))
    kinds = [d["kind"] for d in extract_diagrams(text)]
    assert kinds == ["erDiagram", "flowchart", "sequenceDiagram"]


def test_a_shorter_fence_does_not_close_a_longer_one():
    [d] = extract_diagrams(_fenced("erDiagram\n  A ||--o{ B : x\n```", fence="````"))
    assert d["source"].endswith("```")


def test_empty_oversized_and_excess_blocks_are_dropped():
    assert extract_diagrams(_fenced("   ")) == []
    assert extract_diagrams(_fenced("erDiagram\n" + "x" * MAX_SOURCE_CHARS)) == []
    many = "".join(_fenced(f"erDiagram\n  A{i} ||--o{{ B : x") for i in range(MAX_DIAGRAMS + 3))
    assert len(extract_diagrams(many)) == MAX_DIAGRAMS


def test_unclosed_fence_is_not_a_diagram():
    assert extract_diagrams("```mermaid\nerDiagram\n  A ||--o{ B : x\n") == []


# --- lint_diagrams ----------------------------------------------------------------------------------
from domain.diagrams import lint_diagrams  # noqa: E402

GOOD_ER = """---
title: ok
---
erDiagram
    %% a comment
    A {
        int id PK "key"
        decimal(10_0) amount
        string code FK, UK
    }
    B {
        string a_id
    }
    A ||--o{ B : "id = a_id"
    B }o..o| A : word
"""


def _doc(*blocks: str) -> str:
    return "intro\n\n" + "\n".join(_fenced(b.rstrip("\n")) for b in blocks)


def test_lint_passes_a_valid_er_diagram():
    [r] = lint_diagrams(_doc(GOOD_ER))
    assert r["kind"] == "erDiagram" and r["linted"] and r["errors"] == [] and r["warnings"] == []


def test_lint_reports_a_missing_left_marker_on_its_file_line():
    # The exact mistake from the first real run: "A ..|| B", no left cardinality.
    text = _doc(GOOD_ER.replace('B }o..o| A : word', 'B ..|| A : "builds"'))
    [r] = lint_diagrams(text)
    [err] = r["errors"]
    assert "left marker" in err["problem"]
    assert text.splitlines()[err["line"] - 1].strip() == 'B ..|| A : "builds"'


def test_lint_catches_bad_attributes_names_labels_and_unclosed_blocks():
    bad = """erDiagram
    Bad {
        decimal(18,2) amount
        string two words here
    }
    Spaced Name ||--|| A : x
    A ||--|| B : unquoted label
    Open {
        int x
"""
    problems = " | ".join(e["problem"] for e in lint_diagrams(_doc(bad))[0]["errors"])
    assert "not a valid attribute" in problems
    assert "not an entity block" in problems or "entity name" in problems
    assert "label after ':'" in problems
    assert "never closed" in problems


def test_lint_warns_on_long_labels_and_undeclared_entities():
    src = 'erDiagram\n    A {\n        int id\n    }\n    A ||--o{ Ghost : "' + "x" * 130 + '"\n'
    warnings = " | ".join(w["problem"] for w in lint_diagrams(_doc(src))[0]["warnings"])
    assert "keep labels short" in warnings and "'Ghost' has no entity block" in warnings


def test_lint_requires_the_header_and_leaves_other_kinds_unlinted():
    [r] = lint_diagrams(_doc("flowchart LR\n  a --> b"))
    assert r["linted"] is False and r["errors"] == []
    [r] = lint_diagrams(_doc("---\ntitle: t\n---\nerDiagramX\n  A ||--|| B : x"))
    assert any("exactly 'erDiagram'" in e["problem"] for e in r["errors"])
