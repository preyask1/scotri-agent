# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""domain/mirror_text.py — the notebook text view and the path/content search mirror-find/-grep use."""
from __future__ import annotations

import json
import re

import pytest

from domain.mirror_text import (
    MANIFEST_NAME,
    as_text,
    find_paths,
    grep_text,
    is_searchable,
    notebook_to_text,
    parse_line_range,
    slice_lines,
)


def _nb(*sources):
    return json.dumps({"cells": [{"cell_type": "code", "source": s, "outputs": [{"text": "OUTPUT"}]} for s in sources]})


def test_notebook_renders_cells_and_drops_outputs():
    text = notebook_to_text(_nb(["a = 1\n", "b = 2"], "select 1"))
    assert "# ── cell 0 [code] ──\na = 1\nb = 2" in text
    assert "# ── cell 1 [code] ──\nselect 1" in text
    assert "OUTPUT" not in text


def test_non_notebook_content_passes_through_unchanged():
    assert notebook_to_text("not json") == "not json"
    assert notebook_to_text('{"no": "cells"}') == '{"no": "cells"}'
    assert as_text("x.py", '{"cells": []}') == '{"cells": []}'  # only .ipynb is rendered


def test_grep_line_numbers_match_the_sliced_text_view():
    text = as_text("n.ipynb", _nb("x = 1", "df.saveAsTable('gold.visit_summary')"))
    [hit] = grep_text("n.ipynb", text, re.compile("visit_summary"), 10)
    sliced, total = slice_lines(text, hit.line, hit.line)
    assert "visit_summary" in sliced and total == len(text.splitlines())


@pytest.mark.parametrize("spec,expected", [("120-200", (120, 200)), ("7", (7, 7)), (" 3 - 4 ", (3, 4))])
def test_parse_line_range(spec, expected):
    assert parse_line_range(spec) == expected


@pytest.mark.parametrize("spec", ["", "a-b", "0-3", "9-2", "-5"])
def test_parse_line_range_rejects_bad_input(spec):
    with pytest.raises(ValueError):
        parse_line_range(spec)


def test_find_paths_substring_glob_and_limit():
    paths = ["Gold_Notebooks/OTHER/Visit summary report.ipynb", "Gold/visit_detail.ipynb", "pipeline/PL_Forecast.json"]
    assert find_paths(paths, "visit summary", 10) == ([paths[0]], False)
    assert find_paths(paths, "*.ipynb", 10) == (paths[:2], False)
    assert find_paths(paths, "pl_forecast.json", 10) == ([paths[2]], False)  # case-insensitive
    assert find_paths(paths, "*visit*", 1) == ([paths[0]], True)


def test_grep_respects_remaining_budget():
    hits = grep_text("f", "x\nx\nx", re.compile("x"), 2)
    assert [h.line for h in hits] == [1, 2]


def test_is_searchable_skips_binaries_and_the_manifest():
    assert is_searchable("a/report.json")
    assert not is_searchable("a/logo.PNG")
    assert not is_searchable(MANIFEST_NAME)
