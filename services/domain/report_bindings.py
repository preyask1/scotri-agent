# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Compact summaries of Power BI report definitions (report-bindings, OPEN_ITEMS.md #64). Pure
functions: no filesystem, no network. The CLI hands in file paths and contents.

Why this exists: finding the UAT reference visual for a change meant reading report files whole. A
PBIR visual.json is ~200 lines of which ~20 say what it binds, and a legacy report.json packs every
visual's config into one escaped JSON string per line. Parsing here returns only the page, the
visual type and title, the fields on each role, and the filters, for both layouts. That is roughly
30-60 tokens per visual instead of 1-2k, and it reads both layouts the same way every time.

Field notation: `Table.Field` for a column, `Table.Field (measure)` for a model measure,
`Table.Field (report measure)` for one defined in the report itself (reportExtensions.json / a
legacy modelExtension), `Sum(Table.Field)` for an aggregation, `Table.Hierarchy.Level` for a
hierarchy level. Table names are semantic-model table names, not gold-table names.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

REPORT_SUFFIX = ".Report"
_AGG = {0: "Sum", 1: "Avg", 2: "Count", 3: "Min", 4: "Max", 5: "CountNonNull", 6: "Median",
        7: "StdDev", 8: "Var"}
_CMP = {0: "=", 1: ">", 2: ">=", 3: "<", 4: "<="}
_MAX_VALUES = 10
_DECORATIVE = {"actionButton", "textbox", "shape", "image", "basicShape", "pageNavigator",
               "bookmarkNavigator"}


def report_folders(paths: list[str]) -> dict[str, list[str]]:
    """Group a tree's file paths by the `<...>.Report` folder they sit in."""
    out: dict[str, list[str]] = {}
    for path in paths:
        parts = path.split("/")
        for i, part in enumerate(parts[:-1]):
            if part.endswith(REPORT_SUFFIX):
                out.setdefault("/".join(parts[:i + 1]), []).append(path)
                break
    return out


def definition_files(report: str, paths: list[str]) -> list[str]:
    """The files a summary needs: definition.pbir, the PBIR definition JSON, or the legacy
    report.json. Static resources (images, themes) are never read."""
    keep = []
    for path in paths:
        rel = path[len(report) + 1:]
        if rel == "definition.pbir" or rel == "report.json":
            keep.append(path)
        elif rel.startswith("definition/") and rel.endswith(".json") and not rel.startswith("definition/bookmarks/"):
            keep.append(path)
    return keep


# ── expression helpers ──────────────────────────────────────────────────────────────────────

def _literal(expr) -> str:
    value = (expr or {}).get("Literal", {}).get("Value")
    if value is None:
        return "?"
    value = str(value)
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)[LDM]", value)
    return match.group(1) if match else value


def _entity(source_ref: dict, aliases: dict) -> str:
    if "Entity" in source_ref:
        return source_ref["Entity"]
    return aliases.get(source_ref.get("Source", ""), source_ref.get("Source", "?"))


def field_name(expr: dict, aliases: dict | None = None, report_measures: frozenset = frozenset()) -> str:
    """One field expression (Column / Measure / Aggregation / HierarchyLevel) as compact text."""
    aliases = aliases or {}
    if not isinstance(expr, dict):
        return "?"
    if "Column" in expr:
        col = expr["Column"]
        return f"{_entity(col.get('Expression', {}).get('SourceRef', {}), aliases)}.{col.get('Property', '?')}"
    if "Measure" in expr:
        m = expr["Measure"]
        ref = m.get("Expression", {}).get("SourceRef", {})
        name = f"{_entity(ref, aliases)}.{m.get('Property', '?')}"
        kind = "report measure" if ref.get("Schema") == "extension" or name in report_measures else "measure"
        return f"{name} ({kind})"
    if "Aggregation" in expr:
        agg = expr["Aggregation"]
        return f"{_AGG.get(agg.get('Function'), 'Agg')}({field_name(agg.get('Expression', {}), aliases, report_measures)})"
    if "HierarchyLevel" in expr:
        hl = expr["HierarchyLevel"]
        hier = hl.get("Expression", {}).get("Hierarchy", {})
        inner = hier.get("Expression", {})
        table = _entity(inner.get("SourceRef", {}), aliases) if "SourceRef" in inner else \
            field_name(inner, aliases).split(" ")[0]
        return f"{table}.{hier.get('Hierarchy', '?')}.{hl.get('Level', '?')}"
    return "?"


def _condition(cond: dict, aliases: dict) -> str:
    if not isinstance(cond, dict):
        return "(complex)"
    if "In" in cond:
        rows = cond["In"].get("Values", [])
        values = [", ".join(_literal(v) for v in row) for row in rows[:_MAX_VALUES]]
        more = f", +{len(rows) - _MAX_VALUES} more" if len(rows) > _MAX_VALUES else ""
        return f"in [{'; '.join(values)}{more}]"
    if "Not" in cond:
        return f"not {_condition(cond['Not'].get('Expression', {}), aliases)}"
    if "Comparison" in cond:
        c = cond["Comparison"]
        return f"{_CMP.get(c.get('ComparisonKind'), '?')} {_literal(c.get('Right'))}"
    for op in ("And", "Or"):
        if op in cond:
            return f"({_condition(cond[op].get('Left'), aliases)} {op.lower()} {_condition(cond[op].get('Right'), aliases)})"
    return "(complex)"


def summarize_filter(flt: dict, report_measures: frozenset = frozenset()) -> str:
    """A filter card (PBIR `filterConfig.filters[]`, legacy `filters` string item) as one line:
    `Table.Field in [A; B]`, or just `Table.Field` when the card has no condition set."""
    inner = flt.get("filter") or {}
    aliases = {f.get("Name"): f.get("Entity") for f in inner.get("From", []) if isinstance(f, dict)}
    target = flt.get("field") or flt.get("expression") or {}
    name = field_name(target, aliases, report_measures) if target else "?"
    where = inner.get("Where") or []
    if not where:
        return name
    if flt.get("type") == "TopN":
        return f"{name} top N"
    conds = [_condition(w.get("Condition", {}), aliases) for w in where]
    return f"{name} {' and '.join(conds)}"


def _filters(raw, report_measures) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            return []
    if isinstance(raw, dict):
        raw = raw.get("filters", [])
    return [summarize_filter(f, report_measures) for f in raw or [] if isinstance(f, dict)]


def _title(objects: dict) -> str | None:
    """The title as displayed, or None when there is none or it's switched off (a leftover title
    text with `show: false` is common, e.g. one copied slicer's title on every slicer)."""
    text, shown = None, True
    for item in (objects or {}).get("title", []) or []:
        props = item.get("properties", {})
        if props.get("text") and text is None:
            text = _literal(props["text"].get("expr"))
        if "show" in props and _literal(props["show"].get("expr")) == "false":
            shown = False
    return text if shown else None


def _slicer_selection(objects: dict, report_measures) -> str | None:
    for item in (objects or {}).get("general", []) or []:
        flt = item.get("properties", {}).get("filter", {}).get("filter")
        if flt and flt.get("Where"):
            summary = summarize_filter({"filter": flt, "field": {}}, report_measures)
            return summary.split(" ", 1)[1] if " " in summary else None
    return None


def _table_of(name: str) -> str:
    """`Sum(T.F)` / `T.F (measure)` / `T.H.L` -> `T`."""
    return re.sub(r"^\w+\(", "", name).split(".", 1)[0]


# ── visuals and pages ───────────────────────────────────────────────────────────────────────

@dataclass
class Visual:
    type: str
    title: str | None
    fields: dict[str, list[str]]
    filters: list[str] = field(default_factory=list)
    hidden: bool = False
    selection: str | None = None

    def to_dict(self) -> dict:
        out = {"type": self.type}
        if self.title:
            out["title"] = self.title
        out["fields"] = self.fields
        if self.filters:
            out["filters"] = self.filters
        if self.selection:
            out["selected"] = self.selection
        if self.hidden:
            out["hidden"] = True
        return out

    def tables(self) -> set[str]:
        names = [f for vs in self.fields.values() for f in vs] + self.filters
        return {_table_of(n) for n in names}

    @property
    def is_slicer(self) -> bool:
        return self.type.lower().endswith("slicer")


@dataclass
class Page:
    name: str
    hidden: bool
    visuals: list[Visual]
    filters: list[str]
    decorative: int = 0


def _pbir_visual(doc: dict, report_measures) -> Visual | None:
    v = doc.get("visual")
    if not isinstance(v, dict):
        return None  # a visualGroup: no bindings of its own
    fields: dict[str, list[str]] = {}
    for role, state in (v.get("query", {}).get("queryState", {}) or {}).items():
        names = [field_name(p.get("field", {}), None, report_measures) for p in state.get("projections", [])]
        if names:
            fields[role] = names
    title = _title(v.get("visualContainerObjects", {})) or _title(v.get("objects", {}))
    return Visual(type=v.get("visualType", "?"), title=title, fields=fields,
                  filters=_filters(doc.get("filterConfig"), report_measures),
                  hidden=bool(doc.get("isHidden")),
                  selection=_slicer_selection(v.get("objects", {}), report_measures))


def _legacy_visual(container: dict, report_measures) -> Visual | None:
    try:
        config = json.loads(container.get("config") or "{}")
    except ValueError:
        return None
    sv = config.get("singleVisual")
    if not isinstance(sv, dict):
        return None
    query = sv.get("prototypeQuery") or {}
    aliases = {f.get("Name"): f.get("Entity") for f in query.get("From", []) if isinstance(f, dict)}
    by_ref = {s.get("Name"): field_name(s, aliases, report_measures) for s in query.get("Select", [])}
    fields: dict[str, list[str]] = {}
    for role, projections in (sv.get("projections") or {}).items():
        names = [by_ref.get(p.get("queryRef"), p.get("queryRef", "?")) for p in projections]
        if names:
            fields[role] = names
    return Visual(type=sv.get("visualType", "?"), title=_title(sv.get("vcObjects", {})), fields=fields,
                  filters=_filters(container.get("filters"), report_measures),
                  hidden=bool(config.get("isHidden")),
                  selection=_slicer_selection(sv.get("objects", {}), report_measures))


def _keep(visual: Visual) -> bool:
    return bool(visual.fields or visual.filters) and visual.type not in _DECORATIVE


@dataclass
class ReportSummary:
    report: str
    format: str
    model: str | None
    filters: list[str]
    pages: list[Page]
    report_measures: list[str]

    def tables(self) -> set[str]:
        out = {_table_of(f) for f in self.filters}
        for page in self.pages:
            out |= {_table_of(f) for f in page.filters}
            for visual in page.visuals:
                out |= visual.tables()
        return out

    def to_dict(self, tables: set[str] | None = None) -> dict:
        """Full summary, or with `tables` only the visuals and filters that touch one of them
        (case-insensitive), plus each page's slicers so a reader knows what can be set."""
        want = {t.lower() for t in tables} if tables else None

        def touches(names):
            return want is None or any(_table_of(n).lower() in want for n in names)

        pages = []
        for page in self.pages:
            visuals = [v.to_dict() for v in page.visuals
                       if want is None or touches(v.tables()) or v.is_slicer]
            if want is not None and not any(touches(v.tables()) for v in page.visuals) \
                    and not touches(page.filters):
                continue
            entry = {"page": page.name}
            if page.hidden:
                entry["hidden"] = True
            if page.filters:
                entry["filters"] = page.filters
            entry["visuals"] = visuals
            if page.decorative:
                entry["decorative_visuals_omitted"] = page.decorative
            pages.append(entry)
        out = {"report": self.report, "name": self.report.rsplit("/", 1)[-1][:-len(REPORT_SUFFIX)],
               "format": self.format, "model": self.model}
        if self.filters:
            out["report_filters"] = self.filters
        measures = [m for m in self.report_measures if touches([m])]
        if measures:
            out["report_measures"] = measures
        out["pages"] = pages
        return out


def model_ref(pbir_text: str | None) -> str | None:
    if not pbir_text:
        return None
    try:
        ref = json.loads(pbir_text).get("datasetReference", {})
    except ValueError:
        return None
    if ref.get("byPath", {}).get("path"):
        return ref["byPath"]["path"]
    if ref.get("byConnection"):
        return "byConnection (published model, not a repo path)"
    return None


def summarize_report(report: str, contents: dict[str, str]) -> ReportSummary:
    """Summarize one `<...>.Report` folder from its definition files (see `definition_files`)."""
    def get(rel):
        return contents.get(f"{report}/{rel}")

    def load(rel):
        text = get(rel)
        try:
            return json.loads(text) if text else None
        except ValueError:
            return None

    model = model_ref(get("definition.pbir"))
    pages_root = f"{report}/definition/pages/"
    if any(p.startswith(pages_root) for p in contents):
        ext = load("definition/reportExtensions.json") or {}
        rmeasures = sorted(f"{e.get('name')}.{m.get('name')}" for e in ext.get("entities", [])
                           for m in e.get("measures", []))
        rm = frozenset(rmeasures)
        order = (load("definition/pages/pages.json") or {}).get("pageOrder", [])
        page_ids = sorted({p[len(pages_root):].split("/", 1)[0] for p in contents
                           if p.startswith(pages_root) and p.count("/") > pages_root.count("/")},
                          key=lambda pid: (order.index(pid) if pid in order else len(order), pid))
        pages = []
        for pid in page_ids:
            page_doc = load(f"definition/pages/{pid}/page.json") or {}
            visuals, decorative = [], 0
            vroot = f"{pages_root}{pid}/visuals/"
            for path in sorted(p for p in contents if p.startswith(vroot) and p.endswith("/visual.json")):
                visual = _pbir_visual(json.loads(contents[path] or "{}"), rm)
                if visual is None:
                    continue
                if _keep(visual):
                    visuals.append(visual)
                else:
                    decorative += 1
            pages.append(Page(name=page_doc.get("displayName", pid),
                              hidden=page_doc.get("visibility") == "HiddenInViewMode",
                              visuals=visuals, filters=_filters(page_doc.get("filterConfig"), rm),
                              decorative=decorative))
        report_doc = load("definition/report.json") or {}
        return ReportSummary(report, "pbir", model, _filters(report_doc.get("filterConfig"), rm), pages, rmeasures)

    doc = load("report.json") or {}
    try:
        config = json.loads(doc.get("config") or "{}")
    except ValueError:
        config = {}
    rmeasures = sorted(f"{e.get('name')}.{m.get('name')}" for x in config.get("modelExtensions", [])
                       for e in x.get("entities", []) for m in e.get("measures", []))
    rm = frozenset(rmeasures)
    pages = []
    for section in sorted(doc.get("sections", []), key=lambda s: s.get("ordinal", 0)):
        try:
            sconfig = json.loads(section.get("config") or "{}")
        except ValueError:
            sconfig = {}
        visuals, decorative = [], 0
        for container in section.get("visualContainers", []):
            visual = _legacy_visual(container, rm)
            if visual is None:
                continue
            if _keep(visual):
                visuals.append(visual)
            else:
                decorative += 1
        pages.append(Page(name=section.get("displayName", section.get("name", "?")),
                          hidden=sconfig.get("visibility") == 1, visuals=visuals,
                          filters=_filters(section.get("filters"), rm), decorative=decorative))
    return ReportSummary(report, "legacy", model, _filters(doc.get("filters"), rm), pages, rmeasures)


def rank_entry(summary: ReportSummary, tables: set[str]) -> dict | None:
    """One row of the cross-report ranking: which of `tables` this report's visuals and filters
    use, and how many visible (not hidden, not on a hidden page) non-slicer visuals bind them, since
    those are what show a number, plus the total including hidden pages, which are often reached
    through navigation buttons rather than the tab bar. None when it uses none of them."""
    want = {t.lower() for t in tables}
    matched = sorted(t for t in summary.tables() if t.lower() in want)
    if not matched:
        return None
    pages = []
    visible = total = 0
    for page in summary.pages:
        hits = [v for v in page.visuals if any(t.lower() in want for t in v.tables())]
        data = [v for v in hits if not v.is_slicer]
        if hits:
            total += len(data)
            if not page.hidden:
                visible += sum(not v.hidden for v in data)
            entry = {"page": page.name, "visuals": len(data), "slicers": len(hits) - len(data)}
            if page.hidden:
                entry["hidden"] = True
            pages.append(entry)
    return {"report": summary.report, "format": summary.format, "model": summary.model,
            "tables_matched": matched, "visible_visuals": visible, "data_visuals": total, "pages": pages}
