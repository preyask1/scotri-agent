"""domain/code_lineage.py — notebook -> table and ADF trigger/pipeline -> notebook edges.

Fixtures copy shapes found in the real mirror (2026-10-12): `{final_catalog}.{gold_schema}.x`
with the schema set in NB_Utils, `# MAGIC` SQL in .py notebooks, a markdown cell naming a table,
storage URLs, a MERGE that reads its own target, and ADF notebook paths under /Repos and /Users."""
from __future__ import annotations

import json

from domain.code_lineage import generate_adf_tree, generate_databricks_tree, notebook_tables, resolve_notebook

UTILS = "catalog = 'acme_prod'\ngold_schema = 'gold'\nsilver_schema = 'slvr'\n"

SALES = json.dumps({"cells": [
    {"cell_type": "markdown", "source": ["Builds acme_prod.gold.not_a_real_dependency\n"]},
    {"cell_type": "code", "source": [
        "%run ../NB_Utils\n",
        "df = spark.sql(f\"SELECT * FROM {final_catalog}.{gold_schema}.Sales_trend_Daily_Monthly_final\")\n",
        "path = f\"abfss://raw@{account}.dfs.core.windows.net/x\"\n",
        "# CREATE OR REPLACE TABLE {final_catalog}.{gold_schema}.commented_out AS\n",
    ]},
    {"cell_type": "code", "source": [
        "spark.sql(f\"\"\"CREATE OR REPLACE TABLE {final_catalog}.{gold_schema}.Sales_Trend_Daily_Target AS\n",
        "SELECT * FROM {catalog}.{silver_schema}.stores_sku s JOIN acme_prod.gold.dim_party p ON 1=1\"\"\")\n",
    ]},
]})

MERGE = """# Databricks notebook source
# MAGIC %sql
# MAGIC MERGE INTO acme_prod.gold.visit_summary t
# MAGIC USING acme_prod.slvr.stores_visit s ON t.id = s.id

# COMMAND ----------

df.write.mode("overwrite").saveAsTable(f"{catalog}.{schema}.{table}")
"""


def _dbx():
    files = {"NB_Utils.py": UTILS, "Gold_Notebooks/Sales/USP_SALES.ipynb": SALES, "Gold_Notebooks/OTHER/Visit summary report.py": MERGE}
    return generate_databricks_tree("databricks@release_main", sorted(files), files.get), files


def _has(g, src, dst, rel):
    return any(e.src == src and e.dst == dst and e.rel == rel for e in g.edges)


def test_notebook_writes_and_reads_with_schema_variables_resolved():
    g, _ = _dbx()
    nb = "notebook:Gold_Notebooks/Sales/USP_SALES.ipynb"
    assert _has(g, nb, "gold_table:sales_trend_daily_target", "produces")
    assert _has(g, nb, "gold_table:sales_trend_daily_monthly_final", "consumes")
    assert _has(g, nb, "slvr_table:stores_sku", "consumes")
    assert _has(g, nb, "gold_table:dim_party", "consumes")
    nodes = {e.dst for e in g.edges if e.src == nb}
    assert not any("not_a_real_dependency" in n or "commented_out" in n or "dfs" in n or "core" in n for n in nodes)
    assert g.stats["shared_schema_vars"] == {"gold_schema": "gold", "silver_schema": "slvr"}


def test_merge_reads_its_target_only_as_a_write_and_dynamic_targets_are_reported():
    g, _ = _dbx()
    nb = "notebook:Gold_Notebooks/OTHER/Visit summary report.py"
    assert _has(g, nb, "gold_table:visit_summary", "produces")
    assert not _has(g, nb, "gold_table:visit_summary", "consumes")
    assert _has(g, nb, "slvr_table:stores_visit", "consumes")
    assert any(u.node == nb and "through variables" in u.reason for u in g.unresolved)


def test_notebook_tables_without_known_schema_ignores_object_attributes():
    writes, reads, _ = notebook_tables("x = spark.sql('select 1')\nrow.value.strip()\n", {"gold"})
    assert writes == {} and reads == {}


def test_resolve_adf_notebook_paths():
    nbs = {"gold_notebooks/other/visit summary report": "Gold_Notebooks/OTHER/Visit summary report.py"}
    assert resolve_notebook("/Workspace/Repos/Prod_X/Acme_Databricks_WS/Gold_Notebooks/OTHER/Visit summary report",
                            nbs) == "Gold_Notebooks/OTHER/Visit summary report.py"
    assert resolve_notebook("/Users/someone@acme.example.com/SalesScript/X", nbs) is None


def test_adf_pipelines_and_triggers():
    _, dbx_files = _dbx()
    pipeline = {"name": "PL_Gold", "properties": {"activities": [
        {"name": "ForEachTable", "type": "ForEach", "typeProperties": {"activities": [
            {"name": "Visit", "type": "DatabricksNotebook", "typeProperties": {
                "notebookPath": "/Repos/Prod/Acme_Databricks_WS/Gold_Notebooks/OTHER/Visit summary report"}},
            {"name": "Dyn", "type": "DatabricksNotebook", "typeProperties": {
                "notebookPath": {"value": "@item().path", "type": "Expression"}}},
            {"name": "Personal", "type": "DatabricksNotebook", "typeProperties": {"notebookPath": "/Users/a@b.com/x"}},
        ]}},
        {"name": "Child", "type": "ExecutePipeline", "typeProperties": {"pipeline": {"referenceName": "PL_Child"}}},
    ]}}
    trigger = {"name": "TR_Daily", "properties": {"pipelines": [{"pipelineReference": {"referenceName": "PL_Gold"}}]}}
    files = {"pipeline/PL_Gold.json": json.dumps(pipeline), "trigger/TR_Daily.json": json.dumps(trigger),
             "dataset/ignored.json": "{}"}
    g = generate_adf_tree("adf@main", sorted(files), files.get, sorted(dbx_files))
    assert _has(g, "adf_pipeline:PL_Gold", "notebook:Gold_Notebooks/OTHER/Visit summary report.py", "feeds")
    assert _has(g, "adf_pipeline:PL_Gold", "adf_pipeline:PL_Child", "triggers")
    assert _has(g, "adf_trigger:TR_Daily", "adf_pipeline:PL_Gold", "triggers")
    reasons = sorted(u.reason.split(": ", 1)[1] for u in g.unresolved)
    assert reasons[0].startswith("notebook not in the mirrored repo (/Users/a@b.com/x)")
    assert reasons[1] == "notebookPath is an expression"
    assert g.stats == {"pipelines": 1, "triggers": 1, "edges": 3, "unresolved": 2}


def test_stopped_and_expired_triggers_keep_their_edges_and_are_listed():
    """NightlyPipelineDev's shape: runtimeState Started, but the recurrence endTime is in the past."""
    from datetime import datetime
    sched = {"recurrence": {"frequency": "Day", "endTime": "2025-09-10T08:43:00"}}
    files = {"trigger/Expired.json": json.dumps({"name": "Expired", "properties": {
                 "runtimeState": "Started", "typeProperties": sched, "pipelines": [{"pipelineReference": {"referenceName": "PL_A"}}]}}),
             "trigger/Off.json": json.dumps({"name": "Off", "properties": {
                 "runtimeState": "Stopped", "pipelines": [{"pipelineReference": {"referenceName": "PL_A"}}]}}),
             "trigger/Live.json": json.dumps({"name": "Live", "properties": {
                 "runtimeState": "Started", "typeProperties": {"recurrence": {"frequency": "Day", "endTime": "2099-01-01T00:00:00"}},
                 "pipelines": [{"pipelineReference": {"referenceName": "PL_A"}}]}})}
    g = generate_adf_tree("adf@main", sorted(files), files.get, [], now=datetime(2026, 10, 13))
    assert all(_has(g, f"adf_trigger:{t}", "adf_pipeline:PL_A", "triggers") for t in ("Expired", "Off", "Live"))
    gaps = {u.node: u.reason for u in g.unresolved}
    assert "schedule ended 2025-09-10" in gaps["adf_trigger:Expired"]
    assert "is Stopped" in gaps["adf_trigger:Off"]
    assert "adf_trigger:Live" not in gaps


def test_notebook_calls_include_and_trigger_edges(tmp_path):
    from adapters.lineage.sqlite_lineage_graph import SqliteLineageGraph
    from domain.lineage import LineageNode

    orchestrator = json.dumps({"cells": [{"cell_type": "code", "source": [
        "%run ../NB_Utils\n",
        "dbutils.notebook.run(\"./Child\", 600, {})\n",
        "dbutils.notebook.run(f\"./{name}\", 600)\n",          # f-string: unresolvable
        "dbutils.notebook.run(child_path, 600)\n",            # variable: unresolvable
    ]}]})
    child = "# Databricks notebook source\n# MAGIC %run /Workspace/Repos/P/Acme_Databricks_WS/NB_Utils\n" \
            "spark.sql(\"INSERT INTO acme_prod.gold.child_out SELECT 1\")\n"
    files = {"NB_Utils.py": UTILS, "Gold_Notebooks/Orchestrator.ipynb": orchestrator, "Gold_Notebooks/Child.py": child}
    g = generate_databricks_tree("databricks@release_main", sorted(files), files.get)
    orch, kid, utils = "notebook:Gold_Notebooks/Orchestrator.ipynb", "notebook:Gold_Notebooks/Child.py", "notebook:NB_Utils.py"
    assert _has(g, orch, utils, "includes") and _has(g, kid, utils, "includes")
    assert _has(g, orch, kid, "triggers")
    assert any(u.node == orch and u.reason.startswith("2 notebook call(s)") for u in g.unresolved)
    assert g.stats["notebook_calls"] == 3

    graph = SqliteLineageGraph(tmp_path / "l.db")
    nodes = {e.src for e in g.edges} | {e.dst for e in g.edges}
    graph.rebuild([LineageNode(n, n.split(":", 1)[0], n.split(":", 1)[1]) for n in nodes], g.edges, "x")
    # A change to NB_Utils reaches every notebook that %runs it, and what they write.
    assert {orch, kid, "gold_table:child_out"} <= set(graph.blast_radius(utils, "downstream", 5).nodes)
    # The child's table traces upstream to the notebook that runs it.
    assert orch in graph.blast_radius("gold_table:child_out", "upstream", 5).nodes


def test_folder_runner_triggers_every_notebook_in_each_listed_folder():
    """NB_Gold_Runner's shape: a base path set in NB_Utils, one folder per priority step, os.listdir and a
    variable dbutils.notebook.run. A commented-out folder runs nothing, and a same-named folder under
    another base (Sales_Data_Wave_Load's copy of the layout) is not confused with it."""
    runner = json.dumps({"cells": [{"cell_type": "code", "source": [
        "def run_notebook(folder, name):\n",
        "    notebook_path = f\"{folder}{name}\"\n",
        "    dbutils.notebook.run(notebook_path, timeout_seconds=6000)\n",
        "folder_name = 'STORES/Wave1/'\n",
        "full_path = f\"{Gold_Notebooks_path}{folder_name}\"\n",
        "path_list = os.listdir(full_path)\n",
        "# folder_name = 'ERP/Wave3/'\n",
        "folder_name = 'Missing/Wave9/'\n",
    ]}]})
    files = {"NB_Utils.py": UTILS, "NB_Gold_Runner.ipynb": runner,
             "NB_Utils_v2.py": 'Gold_Notebooks_path = "/Workspace/Repos/Prod_X/Acme_Databricks_WS/Gold_Notebooks/"\n',
             "Sales_Data_Wave_Load/Gold_Notebooks_Sales_Wave/STORES/Wave1/USP_Decoy.py": "x = 1\n",
             "Gold_Notebooks/STORES/Wave1/USP_A.py": "spark.sql(\"INSERT INTO acme_prod.gold.a SELECT 1\")\n",
             "Gold_Notebooks/STORES/Wave1/USP_B.ipynb": json.dumps({"cells": []}),
             "Gold_Notebooks/STORES/Wave1/Sub/USP_Deeper.py": "x = 1\n",
             "Gold_Notebooks/ERP/Wave3/USP_Off.py": "x = 1\n"}
    g = generate_databricks_tree("databricks@release_main", sorted(files), files.get)
    run = "notebook:NB_Gold_Runner.ipynb"
    assert _has(g, run, "notebook:Gold_Notebooks/STORES/Wave1/USP_A.py", "triggers")
    assert _has(g, run, "notebook:Gold_Notebooks/STORES/Wave1/USP_B.ipynb", "triggers")
    assert not _has(g, run, "notebook:Gold_Notebooks/STORES/Wave1/Sub/USP_Deeper.py", "triggers")
    assert not _has(g, run, "notebook:Gold_Notebooks/ERP/Wave3/USP_Off.py", "triggers")
    assert not _has(g, run, "notebook:Sales_Data_Wave_Load/Gold_Notebooks_Sales_Wave/STORES/Wave1/USP_Decoy.py",
                    "triggers")
    # the variable notebook.run is the folder runner, so it no longer counts as unresolved
    assert not any(u.node == run and "notebook call" in u.reason for u in g.unresolved)


def test_folder_runner_in_one_line_form():
    """NB_Gold_Sales_Wave's shape: the base path and folder in one os.listdir f-string."""
    base = "/Workspace/Repos/Prod_X/Acme_Databricks_WS/Sales_Data_Wave_Load/Gold_Notebooks_Sales_Wave/"
    runner = (f"Gold_Notebooks_Sales_Path = '{base}'\n"
              "pos = sorted(os.listdir(f\"{Gold_Notebooks_Sales_Path}POS/Wave1/\"))\n"
              "pool.map(lambda p: dbutils.notebook.run(f\"{Gold_Notebooks_Sales_Path}POS/Wave1/\"+p, 6000), pos)\n"
              "# old = sorted(os.listdir(f\"{Gold_Notebooks_Sales_Path}POS/Wave2/\"))\n")
    runner = runner.replace("\n", "\r\n")  # the real file has CRLF line endings
    files = {"NB_Utils.py": UTILS, "Sales_Data_Wave_Load/NB_Gold_Sales_Wave.py": runner,
             "Sales_Data_Wave_Load/Gold_Notebooks_Sales_Wave/POS/Wave1/USP_S.py": "x = 1\n",
             "Sales_Data_Wave_Load/Gold_Notebooks_Sales_Wave/POS/Wave2/USP_Off.py": "x = 1\n",
             "Gold_Notebooks/POS/Wave1/USP_Other.py": "x = 1\n"}
    g = generate_databricks_tree("databricks@release_main", sorted(files), files.get)
    run = "notebook:Sales_Data_Wave_Load/NB_Gold_Sales_Wave.py"
    assert _has(g, run, "notebook:Sales_Data_Wave_Load/Gold_Notebooks_Sales_Wave/POS/Wave1/USP_S.py", "triggers")
    assert not _has(g, run, "notebook:Sales_Data_Wave_Load/Gold_Notebooks_Sales_Wave/POS/Wave2/USP_Off.py", "triggers")
    assert not _has(g, run, "notebook:Gold_Notebooks/POS/Wave1/USP_Other.py", "triggers")
