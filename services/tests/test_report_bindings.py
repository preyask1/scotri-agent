"""report-bindings (OPEN_ITEMS.md #64): compact report summaries for PBIR and legacy layouts, the
--table narrowing, and the cross-report ranking. Fixtures follow the shapes read from real mirror
files on 2026-10-07 (Department Sales Overview = PBIR, Category vs Market Share = legacy). Pins:
field notation, filter/slicer conditions, switched-off titles dropped, decorative visuals counted
not listed, hidden pages marked, report measures told apart from model measures, copies grouped,
--model gating the ranking, output redacted."""
from __future__ import annotations

import json

import pytest

import app.cli as cli
from adapters.repo_content.mirror_reader_adapter import MirrorRepoContentReader
from domain.models import Environment, MaintenanceStatus, RepoRef
from domain.report_bindings import field_name, rank_entry, summarize_filter, summarize_report


def _lit(v):
    return {"expr": {"Literal": {"Value": v}}}


def _col(entity, prop, source=False):
    ref = {"Source": entity} if source else {"Entity": entity}
    return {"Column": {"Expression": {"SourceRef": ref}, "Property": prop}}


def _measure(entity, prop, extension=False):
    ref = {"Entity": entity, **({"Schema": "extension"} if extension else {})}
    return {"Measure": {"Expression": {"SourceRef": ref}, "Property": prop}}


def _in_filter(entity, prop, *values):
    return {"name": "f", "field": _col(entity, prop), "type": "Categorical", "filter": {
        "Version": 2, "From": [{"Name": "d", "Entity": entity, "Type": 0}],
        "Where": [{"Condition": {"In": {"Expressions": [_col("d", prop, source=True)],
                                         "Values": [[{"Literal": {"Value": v}}] for v in values]}}}]}}


def _visual(vtype, roles=None, title=None, title_shown=True, filters=None, selection=None, hidden=False):
    v = {"visualType": vtype}
    if roles:
        v["query"] = {"queryState": {r: {"projections": [{"field": f} for f in fs]} for r, fs in roles.items()}}
    if title:
        props = {"text": _lit(f"'{title}'")}
        if not title_shown:
            props["show"] = _lit("false")
        v["visualContainerObjects"] = {"title": [{"properties": props}]}
    if selection:
        v["objects"] = {"general": [{"properties": {"filter": {"filter": selection["filter"]}}}]}
    doc = {"name": "x", "visual": v}
    if filters:
        doc["filterConfig"] = {"filters": filters}
    if hidden:
        doc["isHidden"] = True
    return doc


PBIR = "Sales/Sales Analysis.Report"
LEGACY = "MarketData/Category Share.Report"


def _pbir_contents(model="../Sales Model.SemanticModel"):
    root = f"{PBIR}/definition"
    return {
        f"{PBIR}/definition.pbir": json.dumps({"datasetReference": {"byPath": {"path": model}}}),
        f"{root}/report.json": json.dumps({"filterConfig": {"filters": [_in_filter("Plant", "KKBER", "'AGR1'")]}}),
        f"{root}/reportExtensions.json": json.dumps(
            {"name": "extension", "entities": [{"name": "Sales", "measures": [{"name": "Net Cr"}]}]}),
        f"{root}/pages/pages.json": json.dumps({"pageOrder": ["p2", "p1"]}),
        f"{root}/pages/p1/page.json": json.dumps({"displayName": "Detail", "visibility": "HiddenInViewMode"}),
        f"{root}/pages/p2/page.json": json.dumps({"displayName": "Overview"}),
        f"{root}/pages/p2/visuals/a/visual.json": json.dumps(_visual(
            "card", {"Values": [_measure("Sales", "Net Cr", extension=True)]}, title="Net sales")),
        f"{root}/pages/p2/visuals/b/visual.json": json.dumps(_visual(
            "slicer", {"Values": [_col("Date", "Financial Year")]}, title="HQ/Station", title_shown=False,
            selection=_in_filter("Date", "Financial Year", "'FY 2027'"))),
        f"{root}/pages/p2/visuals/c/visual.json": json.dumps(_visual("actionButton", title="Clear all slicers")),
        f"{root}/pages/p2/visuals/g/visual.json": json.dumps({"name": "g", "visualGroup": {"displayName": "grp"}}),
        f"{root}/pages/p1/visuals/d/visual.json": json.dumps(_visual(
            "pivotTable",
            {"Rows": [_col("Hierarchy", "Party")],
             "Values": [{"Aggregation": {"Expression": _col("Sales", "Qty"), "Function": 0}},
                        _measure("Sales", "YTD Sales")]},
            filters=[{"name": "c", "field": _col("Sales", "Amount"), "type": "Advanced", "filter": {
                "Version": 2, "From": [{"Name": "s", "Entity": "Sales", "Type": 0}],
                "Where": [{"Condition": {"Comparison": {"ComparisonKind": 2, "Left": _col("s", "Amount", True),
                                                        "Right": {"Literal": {"Value": "100L"}}}}}]}},
                     {"name": "e", "field": _col("Hierarchy", "Zone"), "type": "Categorical"}])),
        f"{PBIR}/StaticResources/RegisteredResources/logo.png": "not json",
    }


def _legacy_container(vtype, selects, roles, title=None, hidden=False):
    sv = {"visualType": vtype,
          "prototypeQuery": {"Version": 2, "From": [{"Name": "i", "Entity": "market_share_db", "Type": 0}],
                             "Select": selects},
          "projections": roles}
    if title:
        sv["vcObjects"] = {"title": [{"properties": {"text": _lit(f"'{title}'")}}]}
    config = {"name": "v", "singleVisual": sv, **({"isHidden": True} if hidden else {})}
    return {"config": json.dumps(config), "filters": "[]"}


def _legacy_contents():
    division = {**_col("i", "DIVISION", source=True), "Name": "market_share_db.DIVISION"}
    mat = {"Aggregation": {"Expression": _col("i", "MAT", source=True), "Function": 1}, "Name": "Avg(market_share_db.MAT)"}
    report = {
        "config": json.dumps({"modelExtensions": [{"name": "extension", "entities": [
            {"name": "market_share_db", "measures": [{"name": "Date Flag"}]}]}]}),
        "sections": [
            {"name": "s2", "displayName": "QTR", "ordinal": 1, "config": json.dumps({"visibility": 1}),
             "filters": "[]", "visualContainers": []},
            {"name": "s1", "displayName": "MAT & MTH", "ordinal": 0, "config": "{}",
             "filters": json.dumps([_in_filter("market_share_db", "TYPE", "'VALUE'")]),
             "visualContainers": [
                 _legacy_container("tableEx", [division, mat],
                                   {"Values": [{"queryRef": "market_share_db.DIVISION"}, {"queryRef": "Avg(market_share_db.MAT)"}]},
                                   title="Brands"),
                 {"config": json.dumps({"name": "grp", "singleVisualGroup": {"displayName": "g"}})},
             ]},
        ]}
    return {f"{LEGACY}/definition.pbir": json.dumps({"datasetReference": {"byPath": {"path": "../MarketData Model.SemanticModel"}}}),
            f"{LEGACY}/report.json": json.dumps(report)}


# ── domain ──────────────────────────────────────────────────────────────────────────────────

def test_field_notation():
    assert field_name(_col("T", "F")) == "T.F"
    assert field_name(_col("t", "F", source=True), {"t": "Table"}) == "Table.F"
    assert field_name(_measure("T", "M")) == "T.M (measure)"
    assert field_name(_measure("T", "M", extension=True)) == "T.M (report measure)"
    assert field_name(_measure("T", "M"), None, frozenset({"T.M"})) == "T.M (report measure)"
    assert field_name({"Aggregation": {"Expression": _col("T", "F"), "Function": 4}}) == "Max(T.F)"
    hier = {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"SourceRef": {"Entity": "Date"}},
                                                            "Hierarchy": "Cal"}}, "Level": "Year"}}
    assert field_name(hier) == "Date.Cal.Year"


def test_filter_conditions():
    assert summarize_filter(_in_filter("T", "F", "'A'", "2026L")) == "T.F in [A; 2026]"
    assert summarize_filter({"field": _col("T", "F"), "type": "Categorical"}) == "T.F"
    not_in = _in_filter("T", "F", "null")
    not_in["filter"]["Where"][0]["Condition"] = {"Not": {"Expression": not_in["filter"]["Where"][0]["Condition"]}}
    assert summarize_filter(not_in) == "T.F not in [null]"
    many = summarize_filter(_in_filter("T", "F", *[f"'{i}'" for i in range(12)]))
    assert many.endswith("+2 more]")


def test_pbir_summary():
    s = summarize_report(PBIR, _pbir_contents()).to_dict()
    assert s["format"] == "pbir" and s["name"] == "Sales Analysis"
    assert s["model"] == "../Sales Model.SemanticModel"
    assert s["report_filters"] == ["Plant.KKBER in [AGR1]"]
    assert s["report_measures"] == ["Sales.Net Cr"]
    overview, detail = s["pages"]  # pages.json order, not folder order
    assert overview["page"] == "Overview" and "hidden" not in overview
    assert detail["page"] == "Detail" and detail["hidden"] is True
    card, slicer = overview["visuals"]
    assert card == {"type": "card", "title": "Net sales", "fields": {"Values": ["Sales.Net Cr (report measure)"]}}
    assert "title" not in slicer  # set but switched off
    assert slicer["selected"] == "in [FY 2027]"
    assert overview["decorative_visuals_omitted"] == 1  # the button; the group isn't a visual
    pivot = detail["visuals"][0]
    assert pivot["fields"]["Values"] == ["Sum(Sales.Qty)", "Sales.YTD Sales (measure)"]
    assert pivot["filters"] == ["Sales.Amount >= 100", "Hierarchy.Zone"]


def test_legacy_summary():
    s = summarize_report(LEGACY, _legacy_contents()).to_dict()
    assert s["format"] == "legacy" and s["report_measures"] == ["market_share_db.Date Flag"]
    mat, qtr = s["pages"]  # by ordinal
    assert mat["page"] == "MAT & MTH" and qtr == {"page": "QTR", "hidden": True, "visuals": []}
    assert mat["filters"] == ["market_share_db.TYPE in [VALUE]"]
    assert mat["visuals"] == [{"type": "tableEx", "title": "Brands",
                               "fields": {"Values": ["market_share_db.DIVISION", "Avg(market_share_db.MAT)"]}}]


def test_narrowed_to_a_table_keeps_slicers_and_drops_other_pages():
    s = summarize_report(PBIR, _pbir_contents())
    out = s.to_dict({"hierarchy"})
    assert [p["page"] for p in out["pages"]] == ["Detail"]
    assert "report_measures" not in out  # Sales.Net Cr isn't on a Hierarchy table
    out = s.to_dict({"Date"})
    assert [v["type"] for v in out["pages"][0]["visuals"]] == ["slicer"]


def test_rank_entry_counts_data_visuals_not_slicers():
    s = summarize_report(PBIR, _pbir_contents())
    entry = rank_entry(s, {"Sales", "Date"})
    assert entry["tables_matched"] == ["Date", "Sales"]
    assert entry["visible_visuals"] == 1  # the card; the pivot sits on a hidden page
    assert entry["data_visuals"] == 2
    assert entry["pages"][0] == {"page": "Overview", "visuals": 1, "slicers": 1}
    assert rank_entry(s, {"Nothing"}) is None


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────

class _Registry:
    def __init__(self, refs):
        self._refs = refs

    def all_repo_keys(self):
        return list(self._refs)

    def get(self, key):
        return self._refs.get(key, [])


class _Flag:
    def status(self, repo):
        return MaintenanceStatus(active=False, eta_message="")


class _Secrets:
    def get_optional(self, name, default=None):
        return default


class _Container:
    def __init__(self, root):
        self.mirror_reader = MirrorRepoContentReader(root)
        self.maintenance_flag = _Flag()
        self.secrets = _Secrets()
        self.registry = _Registry({
            "fabric_core": [RepoRef("CORE", "o", "p", "main", Environment.PROD, mirrored=True)],
            "fabric_regional": [RepoRef("MUM", "o", "p", "main", Environment.PROD, mirrored=True)],
        })


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    for repo in ("CORE", "MUM"):
        tree = tmp_path / "mirror-active" / repo / "main"
        contents = {**_pbir_contents(), **_legacy_contents()}
        if repo == "MUM":
            contents = _legacy_contents()  # the same legacy report cloned into a second tree
        for path, text in contents.items():
            target = tree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    monkeypatch.setattr(cli, "build_container", lambda: _Container(tmp_path))
    return tmp_path


def _run(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_cli_one_report_narrowed(mirror, capsys):
    code, out = _run(capsys, "report-bindings", "--repo-key", "fabric_core", "--report", PBIR,
                     "--table", "Hierarchy", "--table", "Ghost")
    assert code == 0 and out["repo"] == "CORE"
    assert [p["page"] for p in out["pages"]] == ["Detail"]
    assert out["tables_not_used"] == ["Ghost"]


def test_cli_missing_report_points_at_mirror_find(mirror, capsys):
    code, out = _run(capsys, "report-bindings", "--repo-key", "fabric_core", "--report", "No/Such.Report")
    assert code == 1 and out["category"] == "not_found" and "mirror-find" in out["message"]


def test_cli_needs_report_or_table(mirror, capsys):
    code, out = _run(capsys, "report-bindings", "--repo-key", "fabric_core")
    assert code == 1 and out["category"] == "configuration"
    code, out = _run(capsys, "report-bindings", "--report", PBIR)
    assert code == 1 and "--repo-key" in out["message"]


def test_cli_ranking_groups_copies_and_honours_model(mirror, capsys):
    code, out = _run(capsys, "report-bindings", "--table", "market_share_db", "--table", "Sales")
    assert code == 0 and out["reports_scanned"] == 3
    reports = {r["report"]: r for r in out["reports"]}
    assert sorted(reports[LEGACY]["locations"]) == ["CORE@main", "MUM@main"]
    assert reports[PBIR]["locations"] == ["CORE@main"]
    code, out = _run(capsys, "report-bindings", "--table", "market_share_db", "--table", "Sales", "--model", "marketdata model")
    assert [r["report"] for r in out["reports"]] == [LEGACY]


def test_cli_output_is_redacted(mirror, capsys):
    tree = mirror / "mirror-active" / "CORE" / "main"
    visual = tree / PBIR / "definition" / "pages" / "p2" / "visuals" / "a" / "visual.json"
    visual.write_text(json.dumps(_visual("card", {"Values": [_col("Sales", "F")]},
                                         title="password=hunter2")), encoding="utf-8")
    _, out = _run(capsys, "report-bindings", "--repo-key", "fabric_core", "--report", PBIR)
    assert "hunter2" not in json.dumps(out)


def test_report_bindings_is_batchable():
    assert "report-bindings" in cli.BATCHABLE_COMMANDS
