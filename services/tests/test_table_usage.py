# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""domain/table_usage.py (table-usage, OPEN_ITEMS.md #68): who writes, reads or mentions a table."""
from __future__ import annotations

import pytest

from domain.mirror_text import notebook_to_text
from domain.table_usage import classify, file_kind, scan_text, summarize, table_pattern

NOTEBOOK = notebook_to_text("""{"cells": [
 {"cell_type": "code", "source": ["df = spark.table('acme_prod.gold.zone_region_map')\\n"]},
 {"cell_type": "code", "source": ["# store_visit_report is rebuilt below\\n",
                                  "out.write.mode('append').saveAsTable(f'{cat}.gold.store_visit_report')\\n"]},
 {"cell_type": "code", "source": ["spark.sql('SELECT * FROM gold.store_visit_report_old')\\n"]}
]}""")


def test_matches_the_last_segment_as_a_whole_identifier():
    p = table_pattern("acme_prod.gold.store_visit_report")
    assert p.search("saveAsTable('gold.store_visit_report')")
    assert not p.search("gold.store_visit_report_old")
    with pytest.raises(ValueError):
        table_pattern("drop table; --")


@pytest.mark.parametrize("line, role", [
    ("df.write.mode('overwrite').saveAsTable('x')", "write"),
    ("INSERT OVERWRITE TABLE gold.x SELECT 1", "write"),
    ("CREATE OR REPLACE TEMP VIEW x AS SELECT 1", "write"),
    ("MERGE INTO gold.x t USING s ON t.id = s.id", "write"),
    ("SELECT a FROM gold.x JOIN y", "read"),
    ("df = spark.table('gold.x')", "read"),
    ("# x is rebuilt nightly", "mention"),
    ("x_cols = ['a', 'b']", "mention"),
])
def test_classifies_lines(line, role):
    assert classify(line) == role


def test_scan_keeps_cells_and_line_numbers_that_match_mirror_read():
    usage = scan_text("Gold/Store_Visit.ipynb", NOTEBOOK, table_pattern("store_visit_report"))
    assert usage.kind == "notebook" and usage.roles == {"write", "mention"}
    hits = [(h["cell"], h["role"]) for h in usage.lines]
    assert hits == [(1, "mention"), (1, "write")]  # the _old table in cell 2 is not this table
    lines = NOTEBOOK.splitlines()
    assert "saveAsTable" in lines[usage.lines[1]["line"] - 1]


def test_report_definitions_are_not_scanned_but_models_and_adf_are():
    assert file_kind("Insights/MIS/VISIT INSIGHTS.Report/report.json") is None
    assert file_kind("Insights/MIS/X.SemanticModel/definition/tables/T.tmdl") == "semantic-model"
    assert file_kind("pipeline/PL_Load.json") == "adf"
    assert file_kind("README.md") is None


def test_a_model_is_one_reader_entry_and_writers_come_first():
    pattern = table_pattern("store_visit_report")
    model = [scan_text(f"M/X.SemanticModel/definition/{f}", "Source = gold.store_visit_report", pattern)
             for f in ("model.tmdl", "tables/T.tmdl")]
    writer = scan_text("nb.py", "df.write.saveAsTable('store_visit_report')", pattern)
    out = summarize([("r", "main", model[0]), ("r", "main", model[1]), ("d", "prod", writer)])
    assert [w["path"] for w in out["writers"]] == ["nb.py"]
    assert [(r["path"], r["roles"]) for r in out["readers"]] == [("M/X.SemanticModel", ["read"])]
    assert [line["file"] for line in out["readers"][0]["lines"]] == ["definition/model.tmdl", "definition/tables/T.tmdl"]
