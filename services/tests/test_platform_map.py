# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""tools/platform_map: the onboarding page is built from the lineage index, registry, client config and
ADF mirror, flags notes that name a pipeline the ADF tree no longer has, and is rebuilt after every
sync swap."""
import importlib.util
import json
import re
import sqlite3
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "platform_map" / "gen_platform_map.py"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_platform_map", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


@pytest.fixture()
def world(tmp_path):
    """One Fabric tree with one model and report over one gold table, built by one notebook that an
    ADF pipeline runs on a 00:30 trigger."""
    registry = tmp_path / "repo_registry.yaml"
    registry.write_text(
        "repos:\n"
        "  - repo_key: fabric_secondary\n    ado_repo: AcmeFabricReports\n"
        "    environments:\n      - {environment: prod, branch: regional_main, mirrored: true}\n"
        "  - repo_key: adf\n    ado_org: ORG\n    ado_project: PROJ\n"
        "    environments:\n      - {environment: prod, ado_repo: AdfProd, branch: main, mirrored: true}\n",
        encoding="utf-8")
    (tmp_path / "client.yaml").write_text(
        "client_name: Acme Retail\ndisplay_name: Scotri\n"
        "catalogs: {prod: acme_prod, dev: acme_dev}\n"
        "repo_kinds: {fabric_secondary: fabric, adf: adf}\n"
        "legacy_repos: [fabric_secondary]\n"
        "fabric_workspaces:\n  - {name: ACME-Fabric-Prod-2, repo_key: fabric_secondary, branch: regional_main}\n"
        "fabric_tenant_id: 11111111-1111-1111-1111-111111111111\n"
        "adf_tenant_id: 22222222-2222-2222-2222-222222222222\n", encoding="utf-8")
    notes = tmp_path / "platform-map-notes.json"
    _write(notes, {"sources": [{"name": "Store POS", "pipelines": ["PL_Visit"]}],
                   "main_run": [{"step": "Ingest", "text": "x", "triggers": ["Tr_Visit"]}]})

    model = "fabric_model:fabric_secondary@regional_main/Field/Work Habit/Visit-Status"
    report = "report:fabric_secondary@regional_main/Field/Work Habit/Visit-Status"
    ftable = "fabric_table:fabric_secondary@regional_main/Field/Work Habit/Visit-Status::visit_summary"
    nb = "notebook:Gold_Notebooks/OTHER/Visit summary report.ipynb"
    reader = "notebook:Gold_Notebooks/OTHER/Visit mailer.ipynb"  # reads the gold table, writes nothing
    measure = model.replace("fabric_model", "measure") + "::[Visits]"
    db = tmp_path / "lineage.db"
    con = sqlite3.connect(db)
    con.executescript("create table nodes(id text primary key, kind text, name text);"
                      "create table edges(src text, dst text, rel text, verified int, source_ref text, updated_at text);"
                      "create table meta(key text primary key, value text);"
                      "create table node_notes(node_id text, kind text, title text, text text, source_ref text, verified int);")
    nodes = [(model, "fabric_model"), (report, "report"), (ftable, "fabric_table"), (nb, "notebook"),
             ("gold_table:visit_summary", "gold_table"), ("slvr_table:stores_visit", "slvr_table"),
             ("test_temp_table:scratch", "test_temp_table"),
             ("adf_pipeline:PL_Visit", "adf_pipeline"),
             (measure, "measure"), (reader, "notebook"),
             (model.replace("fabric_model", "rls_role") + "::Field", "rls_role")]
    con.executemany("insert into nodes values (?,?,?)", [(i, k, i) for i, k in nodes])
    edges = [(ftable, model, "part_of"), (ftable, "gold_table:visit_summary", "consumes"), (report, model, "uses"),
             (nb, "gold_table:visit_summary", "produces"), (nb, "slvr_table:stores_visit", "consumes"),
             (nb, "test_temp_table:scratch", "produces"),
             ("adf_pipeline:PL_Visit", nb, "feeds"), (reader, "gold_table:visit_summary", "consumes")]
    con.executemany("insert into edges values (?,?,?,1,'','')", edges)
    con.execute("insert into meta values ('last_build_at', '2026-10-13T03:32:20+00:00')")
    con.execute("insert into node_notes values (?, 'kpi_definition', '**Visits**', 'Store visits in the period', 'doc.md:12', 0)",
                (measure,))
    con.commit()
    con.close()

    active = tmp_path / "mirror" / "mirror-active"
    adf = active / "AdfProd" / "main"
    _write(adf / "trigger" / "Tr_Visit.json", {"name": "Tr_Visit", "properties": {
        "type": "ScheduleTrigger", "runtimeState": "Started",
        "typeProperties": {"recurrence": {"frequency": "Day", "schedule": {"hours": [0], "minutes": [30]}}},
        "pipelines": [{"pipelineReference": {"referenceName": "PL_Visit"}}]}})
    _write(adf / "linkedService" / "LS_Dbx_Prod.json", {"name": "LS_Dbx_Prod", "properties": {"type": "AzureDatabricks"}})
    _write(adf / "pipeline" / "PL_Visit.json", {"name": "PL_Visit", "properties": {"activities": [
        {"type": "DatabricksNotebook", "linkedServiceName": {"referenceName": "LS_Dbx_Prod"}}]}})
    pages = active / "AcmeFabricReports" / "regional_main" / "Field" / "Work Habit" / "Visit-Status.Report" / "definition" / "pages"
    _write(pages / "pages.json", {"pageOrder": ["p2", "p1"]})
    _write(pages / "p1" / "page.json", {"name": "p1", "displayName": "Individual", "visibility": "HiddenInViewMode"})
    _write(pages / "p2" / "page.json", {"name": "p2", "displayName": "Team View"})
    _write(pages / "p2" / "visuals" / "v1" / "visual.json", {})
    return tmp_path, db, registry, notes


def _data(html: str) -> dict:
    payload = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1)
    return json.loads(payload)


def test_builds_the_page_from_index_registry_client_config_and_mirror(gen, world):
    tmp, db, registry, notes = world
    html, stats = gen.build(db, tmp / "mirror", registry, notes_path=notes)
    data = _data(html)

    assert "/*DATA*/" not in html and "<title>Data Platform Map</title>" in html
    assert data["client"] == {"client_name": "Acme Retail", "display_name": "Scotri"}
    assert data["catalogs"] == {"prod": "acme_prod", "dev": "acme_dev"}
    assert [t["key"] for t in data["trees"]] == ["fabric_secondary@regional_main"]
    assert data["trees"][0]["workspace"] == "ACME-Fabric-Prod-2" and data["trees"][0]["gen"] == "legacy"
    model = data["models"][0]
    assert (model["gold"], model["measures"], model["roles"]) == (["gold_table:visit_summary"], 1, ["Field"])
    report = data["reports"][0]
    assert report["fmt"] == "PBIR" and report["pages"] == [["Team View", 0, 1], ["Individual", 1, 0]]
    table = next(t for t in data["tables"] if t["id"] == "gold_table:visit_summary")
    assert table["producers"] == ["Gold_Notebooks/OTHER/Visit summary report.ipynb"] and len(table["models"]) == 1
    assert not any(t["kind"] == "test_temp_table" for t in data["tables"])  # test/scratch schemas are left out
    assert data["triggers"][0]["when"] == "daily 00:30"
    assert data["pipelines"][0]["notebooks"] == ["Gold_Notebooks/OTHER/Visit summary report.ipynb"]
    assert data["compute"] == [{"ls": "LS_Dbx_Prod", "desc": "", "pipelines": 1}]
    assert stats["reports"] == 1 and stats["stale_names"] == []
    # where things live: every registry repo/branch with its Azure DevOps link, and the tenants
    assert {(r["key"], r["branch"], r["mirrored"]) for r in data["repos"]} == {("fabric_secondary", "regional_main", True),
                                                                           ("adf", "main", True)}
    adf = next(r for r in data["repos"] if r["key"] == "adf")
    assert adf["url"] == "https://dev.azure.com/ORG/PROJ/_git/AdfProd"
    assert data["factories"] == []
    assert [(t["id"], t["holds"]) for t in data["tenants"]] == [
        ("22222222-2222-2222-2222-222222222222", "Azure Data Factory"),
        ("11111111-1111-1111-1111-111111111111", "Fabric and Power BI")]
    assert data["notes"]["sources"][0]["name"] == "Store POS"
    # reader-only notebooks are kept so the page's lineage can walk through them
    assert data["notebooks"]["Gold_Notebooks/OTHER/Visit mailer.ipynb"] == {"in": ["gold_table:visit_summary"], "n_in": 1, "out": []}
    assert data["kpis"] == [{"name": "Visits", "kind": "measure", "object": "[Visits]", "model": model["id"],
                             "text": "Store visits in the period", "verified": 0, "source": "doc.md:12"}]


def test_without_client_config_or_notes_the_page_still_builds(gen, world, tmp_path):
    tmp, db, registry, _notes = world
    (tmp / "client.yaml").unlink()
    html, stats = gen.build(db, tmp / "mirror", registry, notes_path=tmp_path / "absent.json")
    data = _data(html)
    assert data["notes"] == {} and data["tenants"] == [] and data["client"]["client_name"] == ""
    # without repo_kinds, `adf` is the ADF repo by its key and every other repo is a Fabric tree
    assert [t["key"] for t in data["trees"]] == ["fabric_secondary@regional_main"]
    assert data["trees"][0]["workspace"] == "—" and data["trees"][0]["gen"] == "new"
    assert stats["stale_names"] == []


def test_notes_naming_a_missing_pipeline_or_trigger_are_reported(gen):
    notes = {"sources": [{"pipelines": ["PL_Visit", "PL_Gone"]}],
             "main_run": [{"triggers": ["Tr_Gone"], "pipelines": ["PL_Visit"]}],
             "start_here": {"trigger": "Tr_Visit"}}
    assert gen.stale_names(notes, {"PL_Visit"}, {"Tr_Visit"}) == ["pipeline PL_Gone", "trigger Tr_Gone"]


def test_the_template_has_one_data_slot_and_no_stray_control_characters(gen):
    template = gen.TEMPLATE.read_text(encoding="utf-8")
    assert template.count("/*DATA*/") == 1
    assert "\x08" not in template  # a regex \b once arrived as a literal backspace


def test_main_writes_the_page_atomically(gen, world):
    tmp, db, registry, notes = world
    out = tmp / "out" / "platform-map.html"
    assert gen.main(["--out", str(out), "--lineage-db", str(db), "--mirror-root", str(tmp / "mirror"),
                     "--registry", str(registry), "--client", str(tmp / "client.yaml"), "--notes", str(notes)]) == 0
    assert out.is_file() and not out.with_suffix(".tmp").exists()


def test_the_example_notes_are_valid_json_naming_only_known_keys(gen):
    root = Path(__file__).resolve().parents[2]
    known = {"_about", "tagline", "locale", "timezone", "timezone_label", "sources", "ingestion", "main_run_title",
             "main_run", "adf_naming", "layers", "schemas", "repo_layout", "gold_runner", "powerbi_note",
             "powerbi_patterns", "consumers", "environments_note", "platform", "glossary", "start_here"}
    for path in (root / "examples" / "acme-retail" / "docs" / "ai" / "client" / "platform-map-notes.json",
                 root / "services" / "setup_templates" / "platform-map-notes.json"):
        notes = gen.load_notes(path)
        assert set(notes) <= known, path


def test_the_sync_rebuilds_the_map_once_per_run_not_per_tree():
    """Per tree: search cache and lineage. Once per run, after every tree's lineage: the map."""
    text = (Path(__file__).resolve().parents[1] / "app" / "container.py").read_text(encoding="utf-8")
    per_tree = text[text.index("def after_mirror_swap"):text.index("def _refresh_tree_lineage")]
    assert "_refresh_tree_lineage" in per_tree and "rebuild_platform_map" not in per_tree
    per_run = text[text.index("def after_mirror_sync"):text.index("def rebuild_platform_map")]
    assert "self.rebuild_platform_map(" in per_run
