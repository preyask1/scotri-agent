"""Lineage edges generated from the mirror: Fabric semantic models (TMDL) and the reports bound to
them, down to the Databricks tables their partitions load. Pure: no filesystem, no network. The
caller hands in file paths and contents (see `generate_tree`).

What this adds to the hand-curated graph (docs/ai/client/lineage-graph.yaml): every measure, column, RLS
role and report field across every mirrored Fabric tree, rebuilt on each sync, so "what does
changing gold table X / column C touch" is a traversal instead of a read of every model. Edges come
from the files themselves, so they are `verified: true`; each carries the mirror path and line it
came from in `source_ref`.

Node ids (tree = `<repo_key>@<branch>`, model = its path without `.SemanticModel`):
  fabric_model:<tree>/<model>                 report:<tree>/<report path without .Report>
  fabric_table:<tree>/<model>::<table>        column:<tree>/<model>::<table>[<column>]
  measure:<tree>/<model>::[<measure>]         rls_role:<tree>/<model>::<role>
  gold_table:<table>   gold_column:<table>.<column>   (lower-case; `<schema>_table` for non-gold)

Every rel is stored in its natural reading (`measure depends_on column`); traversal turns it into
data-flow direction with `domain.lineage.FLOW_REVERSED_RELS`.

DAX and M are parsed lexically, not evaluated, so some references can't be pinned to a model object
(a virtual column from ADDCOLUMNS, a composite-model entity, a non-Databricks source). Each such
case is listed in `unresolved` rather than dropped, so coverage is stated, not implied.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Callable

from domain.lineage import LineageEdge

MODEL_SUFFIX = ".SemanticModel"
# Bump when the generator's output changes (a parser fix, a new edge or field): stored trees from an
# older version count as stale, so `ensure_lineage_current` regenerates them on the next start.
GENERATOR_VERSION = 6
_AUTO_DATE_PREFIXES = ("LocalDateTable_", "DateTableTemplate_")


# ── TMDL parsing ────────────────────────────────────────────────────────────────────────────

@dataclass
class Expr:
    text: str
    path: str
    line: int  # 1-based line of the declaration


@dataclass
class TableDef:
    name: str
    path: str
    columns: dict[str, Expr | None] = field(default_factory=dict)   # None = data column
    source_columns: dict[str, str] = field(default_factory=dict)    # column -> sourceColumn
    measures: dict[str, Expr] = field(default_factory=dict)
    partitions: list[tuple[str, Expr, dict]] = field(default_factory=list)  # (kind, source, props)
    descriptions: dict[str, str] = field(default_factory=dict)  # "measure:<name>"/"column:<name>" -> `///` text


@dataclass
class ModelDef:
    tables: dict[str, TableDef] = field(default_factory=dict)
    expressions: dict[str, Expr] = field(default_factory=dict)
    roles: dict[str, list[tuple[str, Expr]]] = field(default_factory=dict)  # role -> (table, filter)

    def table(self, name: str) -> TableDef | None:
        t = self.tables.get(name)
        if t is not None:
            return t
        low = name.lower()
        return next((t for n, t in self.tables.items() if n.lower() == low), None)

    def measure_home(self, name: str) -> str | None:
        low = name.lower()
        for t in self.tables.values():
            for m in t.measures:
                if m.lower() == low:
                    return m
        return None


_DECL_RE = re.compile(
    r"^(\t*)(table|column|measure|partition|expression|role|tablePermission|calculationItem)\s+"
    r"('(?:[^']|'')*'|[^\s=']+(?: [^\s=']+)*?)\s*(?:=\s*(.*))?$")
_PROP_RE = re.compile(r"^(\t*)([A-Za-z]+)\s*:\s*(.*)$")
_SOURCE_RE = re.compile(r"^(\t*)source\s*=\s*(.*)$")


def _unquote(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
        return name[1:-1].replace("''", "'")
    return name


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip("\t"))


def _capture(lines: list[str], i: int, first: str, base: int) -> tuple[str, int]:
    """The expression starting with `first` (text after `=` on line i), plus its continuation:
    a ``` block up to the closing fence, else every following line indented deeper than `base`
    (blank lines included). Returns (text, index of the next unconsumed line)."""
    if first.strip().startswith("```"):
        body, j = [], i + 1
        while j < len(lines) and lines[j].strip() != "```":
            body.append(lines[j])
            j += 1
        return "\n".join(body), j + 1
    body, j = [first], i + 1
    while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > base):
        body.append(lines[j])
        j += 1
    return "\n".join(body).strip(), j


def _description_above(lines: list[str], i: int) -> str:
    """The `///` description block directly above line i (TMDL's object description), or ""."""
    out = []
    k = i - 1
    while k >= 0 and lines[k].strip().startswith("///"):
        out.append(lines[k].strip()[3:].strip())
        k -= 1
    return " ".join(reversed(out)).strip()


def parse_tmdl(files: dict[str, str]) -> ModelDef:
    """One model from its `definition/**.tmdl` files (path -> text)."""
    model = ModelDef()
    for path in sorted(files):
        lines = files[path].splitlines()
        table: TableDef | None = None
        role: str | None = None
        current: tuple[str, str] | None = None  # (kind, name) of the indent-1 object
        i = 0
        while i < len(lines):
            line = lines[i]
            m = _DECL_RE.match(line)
            if m:
                depth, kind, name, rhs = len(m.group(1)), m.group(2), _unquote(m.group(3)), m.group(4)
                if depth == 0 and kind == "table":
                    table = model.tables.setdefault(name, TableDef(name, path))
                    i += 1
                    continue
                if depth == 0 and kind == "role":
                    role = name
                    model.roles.setdefault(role, [])
                    i += 1
                    continue
                if depth == 0 and kind == "expression":
                    decl_line = i + 1
                    text, i = _capture(lines, i, rhs or "", 1)
                    model.expressions[name] = Expr(text, path, decl_line)
                    continue
                if depth == 1 and table is not None and kind in ("column", "measure", "partition"):
                    decl_line = i + 1
                    current = (kind, name)
                    if kind in ("column", "measure") and (desc := _description_above(lines, i)):
                        table.descriptions[f"{kind}:{name}"] = desc
                    if kind == "column":
                        if rhs is not None and rhs.strip():
                            text, i = _capture(lines, i, rhs, 2)
                            table.columns[name] = Expr(text, path, decl_line)
                        elif rhs is not None:  # `column X =` with the expression on following lines
                            text, i = _capture(lines, i, "", 2)
                            table.columns[name] = Expr(text, path, decl_line)
                        else:
                            table.columns[name] = None
                            i += 1
                        continue
                    if kind == "measure":
                        text, i = _capture(lines, i, rhs or "", 2)
                        table.measures[name] = Expr(text, path, decl_line)
                        continue
                    if kind == "partition":
                        props, src, j = {}, None, i + 1
                        while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > 1):
                            sm = _SOURCE_RE.match(lines[j])
                            if sm and len(sm.group(1)) == 2:
                                src_line = j + 1
                                text, j = _capture(lines, j, sm.group(2), 2)
                                src = Expr(text, path, src_line)
                                continue
                            pm = _PROP_RE.match(lines[j])
                            if pm:
                                props[pm.group(2)] = pm.group(3).strip()
                            j += 1
                        table.partitions.append(((rhs or "").strip(), src or Expr("", path, decl_line), props))
                        i = j
                        continue
                if depth == 1 and role is not None and kind == "tablePermission":
                    decl_line = i + 1
                    text, i = _capture(lines, i, rhs or "", 2)
                    model.roles[role].append((name, Expr(text, path, decl_line)))
                    continue
            pm = _PROP_RE.match(line)
            if pm and table is not None and current and current[0] == "column" and len(pm.group(1)) == 2 \
                    and pm.group(2) == "sourceColumn":
                table.source_columns[current[1]] = _unquote(pm.group(3))
            i += 1
    return model


# ── DAX references ──────────────────────────────────────────────────────────────────────────

_DAX_TOKEN = re.compile(
    r"""(?P<comment>//[^\n]*|--[^\n]*|/\*.*?\*/)
      | (?P<string>"(?:[^"]|"")*")
      | (?P<qname>'(?:[^']|'')*')
      | (?P<bracket>\[(?:[^\]]|\]\])*\])
      | (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
      | (?P<other>\S)""", re.X | re.S)


def dax_tokens(text: str) -> list[tuple[str, str]]:
    out = []
    for m in _DAX_TOKEN.finditer(text):
        kind = m.lastgroup
        if kind in ("comment", "string"):
            continue
        value = m.group(kind)
        if kind == "qname":
            value = value[1:-1].replace("''", "'")
        elif kind == "bracket":
            value = value[1:-1].replace("]]", "]")
        out.append((kind, value))
    return out


@dataclass(frozen=True)
class Ref:
    kind: str   # "column" | "measure" | "table"
    table: str  # canonical table name ("" for a measure)
    name: str   # column or measure name ("" for a table)


def dax_refs(text: str, model: ModelDef, home: str | None) -> tuple[set[Ref], list[str]]:
    """Model objects a DAX expression references, resolved case-insensitively against `model`.
    `home` is the table the expression lives on, for a bare `[x]` in row context. Returns the
    resolved refs and the references that resolved to nothing in the model."""
    refs: set[Ref] = set()
    unresolved: list[str] = []
    toks = dax_tokens(text)
    for k, (kind, value) in enumerate(toks):
        nxt = toks[k + 1] if k + 1 < len(toks) else ("", "")
        prev = toks[k - 1] if k else ("", "")
        if kind in ("qname", "ident") and nxt[0] == "bracket":
            t = model.table(value)
            if t is None:
                if kind == "qname":
                    unresolved.append(f"table '{value}' not in model (via '{value}'[{nxt[1]}])")
                continue
            col = _match(t.columns, nxt[1])
            if col:
                refs.add(Ref("column", t.name, col))
            elif (m := model.measure_home(nxt[1])):
                refs.add(Ref("measure", "", m))
            else:
                unresolved.append(f"column [{nxt[1]}] not in table '{t.name}'")
        elif kind == "bracket" and prev[0] not in ("qname", "ident"):
            if (m := model.measure_home(value)):
                refs.add(Ref("measure", "", m))
            elif home and (t := model.table(home)) and (col := _match(t.columns, value)):
                refs.add(Ref("column", t.name, col))
            # else: a virtual column (ADDCOLUMNS/SUMMARIZE alias) or a VAR table's column — not a
            # model object, and not a gap in coverage.
        elif kind == "qname":
            t = model.table(value)
            if t is not None:
                refs.add(Ref("table", t.name, ""))
            else:
                unresolved.append(f"table '{value}' not in model")
        elif kind == "ident" and nxt != ("other", "(") and (t := model.table(value)) is not None:
            refs.add(Ref("table", t.name, ""))
    return refs, unresolved


def _match(names, wanted: str) -> str | None:
    if wanted in names:
        return wanted
    low = wanted.lower()
    return next((n for n in names if n.lower() == low), None)


# ── M (Power Query) sources ─────────────────────────────────────────────────────────────────

# `Name="gold"` or `Name=SchemaName` / `Name=#"Schema Name"`, a parameter resolved via `params`.
_NAV_RE = re.compile(r'Name\s*=\s*(?:"([^"]+)"|#"([^"]+)"|([A-Za-z_]\w*))\s*,\s*Kind\s*=\s*"(Database|Schema|Table|View)"')
_PARAM_RE = re.compile(r'^\s*"([^"]*)"\s*(?:meta\b|$)')
_CATALOG_OPT_RE = re.compile(r'Catalog\s*=\s*"([^"]+)"')
_M_STRING_RE = re.compile(r'"(?:[^"]|"")*"')
# Catalog names come from the deployment (config/client.yaml `catalogs`); with none configured, a
# name ending _prod/_dev/_uat/_qa/_test, the usual Unity Catalog convention. A bare three-part
# `a.b.c` isn't taken as catalog.schema.table without that: `schema.table.column` looks the same.
DEFAULT_CATALOG_PATTERN = r"[A-Za-z_]\w*_(?:prod|dev|uat|qa|test)"


def catalog_pattern(catalogs=()) -> str:
    """A regex alternation matching this deployment's catalog names (case-insensitive use)."""
    names = sorted({c for c in catalogs if c}, key=len, reverse=True)
    return "|".join(re.escape(c) for c in names) if names else DEFAULT_CATALOG_PATTERN


def three_part_re(catalogs=()) -> re.Pattern:
    return re.compile(rf"(?<![\w.`])`?({catalog_pattern(catalogs)})`?\.`?(\w+)`?\.`?(\w+)`?", re.I)


_THREE_PART_RE = three_part_re()
_TWO_PART_RE = re.compile(r"(?<![\w.`])`?(gold|silver|bronze)`?\.`?(\w+)`?", re.I)
_CONNECTOR_RE = re.compile(r"\b([A-Z][A-Za-z]+(?:\.[A-Z][A-Za-z]+)+)\s*\(")
# Data entered in the model itself ("Enter data" tables, generated lists): no upstream to trace.
_STATIC_CONNECTORS = ("#table", "Table.FromRows", "Table.FromRecords", "Table.FromList", "List.Dates",
                      "List.Numbers", "Json.Document", "Binary.Decompress", "Binary.FromText")


@dataclass(frozen=True)
class DbxTable:
    catalog: str
    schema: str
    table: str

    @property
    def node(self) -> str:
        kind = "gold_table" if self.schema == "gold" else f"{self.schema}_table"
        return f"{kind}:{self.table}"


@dataclass
class MSource:
    tables: set[DbxTable] = field(default_factory=set)
    queries: set[str] = field(default_factory=set)  # other tables/expressions of the same model
    navigation_only: bool = False  # tables came only from navigation steps (no SQL), no renames
    connectors: set[str] = field(default_factory=set)
    unresolved_params: set[str] = field(default_factory=set)  # navigation names not resolvable


def string_params(expressions: dict[str, "Expr"]) -> dict[str, str]:
    """Model parameters whose value is a string literal (`expression SchemaName = "gold" meta
    [IsParameterQuery=true, ...]`), for navigation steps that name a parameter."""
    return {n: m.group(1) for n, e in expressions.items() if (m := _PARAM_RE.match(e.text))}


def m_source(text: str, query_names: set[str], self_name: str, params: dict[str, str] | None = None,
             three_part: re.Pattern = _THREE_PART_RE) -> MSource:
    out = MSource()
    params = params or {}
    out.connectors = set(_CONNECTOR_RE.findall(text))
    # The catalog only labels the table; node ids are schema + table, so an unnamed one stays "".
    default_catalog = (_CATALOG_OPT_RE.search(text) or [None, ""])[1]
    db, schema = default_catalog, None
    nav = set()
    for literal, hashed, ident, kind in _NAV_RE.findall(text):
        name = literal or params.get(hashed or ident)
        if name is None:
            out.unresolved_params.add(hashed or ident)
            continue
        if kind == "Database":
            db, schema = name, None
        elif kind == "Schema":
            schema = name
        elif schema:
            nav.add(DbxTable(db.lower(), schema.lower(), name.lower()))
    sql = set()
    for s in _M_STRING_RE.findall(text):
        body = s[1:-1].replace('""', '"').replace("#(lf)", "\n").replace("#(cr)", " ").replace("#(tab)", " ")
        for c, sch, t in three_part.findall(body):
            sql.add(DbxTable(c.lower(), sch.lower(), t.lower()))
        stripped = three_part.sub(" ", body)
        for sch, t in _TWO_PART_RE.findall(stripped):
            sql.add(DbxTable(default_catalog.lower(), sch.lower(), t.lower()))
    out.tables = nav | sql
    out.navigation_only = bool(nav) and not sql and "RenameColumns" not in text
    code = _M_STRING_RE.sub('""', text)
    for q in query_names:
        if q == self_name:
            continue
        if f'#"{q}"' in text or (re.fullmatch(r"[A-Za-z_]\w*", q) and re.search(rf"(?<![\w.#\"]){re.escape(q)}(?![\w\"])", code)):
            out.queries.add(q)
    return out


# ── edges for one tree ──────────────────────────────────────────────────────────────────────

@dataclass
class Unresolved:
    node: str
    reason: str
    source_ref: str

    def to_dict(self) -> dict:
        return {"node": self.node, "reason": self.reason, "source_ref": self.source_ref}


@dataclass
class TreeGraph:
    tree: str
    edges: list[LineageEdge] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    locations: dict[str, str] = field(default_factory=dict)  # node -> "<tree>:<path>:<line>" it is defined at
    descriptions: dict[str, str] = field(default_factory=dict)  # node -> the model's own `///` description


def model_folders(paths: list[str]) -> dict[str, list[str]]:
    """`<...>.SemanticModel` folder -> its `definition/**.tmdl` files."""
    out: dict[str, list[str]] = {}
    for path in paths:
        if not path.endswith(".tmdl") or "/definition/" not in path:
            continue
        parts = path.split("/")
        for i, part in enumerate(parts[:-1]):
            if part.endswith(MODEL_SUFFIX):
                out.setdefault("/".join(parts[:i + 1]), []).append(path)
                break
    return out


class _Builder:
    def __init__(self, tree: str) -> None:
        self.g = TreeGraph(tree)
        self._seen: set[tuple[str, str, str]] = set()

    def edge(self, src: str, dst: str, rel: str, ref: str) -> None:
        if src == dst or (src, dst, rel) in self._seen:
            return
        self._seen.add((src, dst, rel))
        self.g.edges.append(LineageEdge(src=src, dst=dst, rel=rel, verified=True, source_ref=ref))

    def gap(self, node: str, reason: str, ref: str) -> None:
        self.g.unresolved.append(Unresolved(node, reason, ref))


def _ids(tree: str, model_path: str):
    base = f"{tree}/{model_path[:-len(MODEL_SUFFIX)]}"
    return (base,
            lambda t: f"fabric_table:{base}::{t}",
            lambda t, c: f"column:{base}::{t}[{c}]",
            lambda m: f"measure:{base}::[{m}]")


def model_edges(b: _Builder, model_path: str, model: ModelDef, catalogs=()) -> None:
    tree = b.g.tree
    base, tid, cid, mid = _ids(tree, model_path)
    model_node = f"fabric_model:{base}"
    query_names = set(model.tables) | set(model.expressions)
    params = string_params(model.expressions)
    three_part = three_part_re(catalogs) if catalogs else _THREE_PART_RE

    def ref(e: Expr) -> str:
        return f"{tree}:{e.path}:{e.line}"

    def link(src: str, refs: set[Ref], e: Expr) -> None:
        for r in refs:
            dst = (cid(r.table, r.name) if r.kind == "column" else mid(r.name) if r.kind == "measure"
                   else tid(r.table))
            b.edge(src, dst, "depends_on", ref(e))

    expr_tables: dict[str, set[DbxTable]] = {}
    for name, e in model.expressions.items():
        expr_tables[name] = m_source(e.text, set(), name, params, three_part).tables

    for t in model.tables.values():
        if t.name.startswith(_AUTO_DATE_PREFIXES):
            continue
        node = tid(t.name)
        b.g.locations[node] = f"{tree}:{t.path}:1"
        b.edge(node, model_node, "part_of", f"{tree}:{t.path}:1")
        single: DbxTable | None = None
        for kind, src, props in t.partitions:
            sref = ref(src)
            if kind == "m":
                ms = m_source(src.text, query_names, t.name, params, three_part)
                tables = set(ms.tables)
                if ms.unresolved_params:
                    b.gap(node, "M navigation names a non-literal parameter: "
                                + ", ".join(sorted(ms.unresolved_params)), sref)
                for q in ms.queries:
                    if q in model.tables:
                        b.edge(node, tid(q), "consumes", sref)
                    elif expr_tables.get(q):
                        tables |= expr_tables[q]
                for d in tables:
                    b.edge(node, d.node, "consumes", sref)
                if len(tables) == 1 and ms.navigation_only:
                    single = next(iter(tables))
                if not tables and not ms.queries and not any(s in src.text for s in _STATIC_CONNECTORS):
                    external = sorted(c for c in ms.connectors
                                      if not c.startswith(("Table.", "List.", "Text.", "Date.", "Number.",
                                                           "Record.", "Value.", "Splitter.", "Combiner.",
                                                           "Replacer.", "Comparer.", "Duration.", "DateTime.",
                                                           "Int64.", "Logical.", "Character.", "Lines.")))
                    if external:
                        b.gap(node, f"M source not resolved to a Databricks table (connectors: {', '.join(external)})", sref)
                    else:
                        b.gap(node, "M source references nothing recognisable", sref)
            elif kind == "entity":
                if props.get("schemaName") and props.get("entityName"):
                    d = DbxTable("", props["schemaName"].strip("'").lower(), props["entityName"].strip("'").lower())
                    b.edge(node, d.node, "consumes", sref)
                else:
                    via = props.get("expressionSource", "?").strip("'")
                    b.gap(node, f"entity partition over another model ({via}); its source is not in this tree", sref)
            elif kind == "calculated":
                refs, gaps = dax_refs(src.text, model, t.name)
                link(node, refs, src)
                for g_ in gaps:
                    b.gap(node, f"DAX: {g_}", sref)
        for key, desc in t.descriptions.items():
            kind, _, name = key.partition(":")
            b.g.descriptions[cid(t.name, name) if kind == "column" else mid(name)] = desc
        for col, e in t.columns.items():
            cnode = cid(t.name, col)
            if e is not None:
                b.g.locations[cnode] = ref(e)
            b.edge(node, cnode, "contains", f"{tree}:{t.path}:1")
            if e is not None:
                refs, gaps = dax_refs(e.text, model, t.name)
                link(cnode, refs, e)
                for g_ in gaps:
                    b.gap(cnode, f"DAX: {g_}", ref(e))
            elif single and (sc := t.source_columns.get(col)):
                gold_col = f"gold_column:{single.table}.{sc.lower()}"
                b.edge(single.node, gold_col, "contains", f"{tree}:{t.path}:1")
                b.edge(cnode, gold_col, "consumes", f"{tree}:{t.path}:1")
        for name, e in t.measures.items():
            mnode = mid(name)
            b.g.locations[mnode] = ref(e)
            refs, gaps = dax_refs(e.text, model, t.name)
            link(mnode, refs, e)
            for g_ in gaps:
                b.gap(mnode, f"DAX: {g_}", ref(e))

    for role, perms in model.roles.items():
        rnode = f"rls_role:{base}::{role}"
        if perms:
            b.g.locations[rnode] = ref(perms[0][1])
        for table, e in perms:
            if model.table(table):
                b.edge(rnode, tid(model.table(table).name), "depends_on", ref(e))
            refs, gaps = dax_refs(e.text, model, table)
            link(rnode, refs, e)
            for g_ in gaps:
                b.gap(rnode, f"DAX: {g_}", ref(e))


def resolve_field(text: str, model: ModelDef) -> Ref | None:
    """A report field string (report_bindings notation, possibly with a filter condition after
    it) resolved against the model by longest table name, then longest column/measure name."""
    s = text.strip()
    if " (report measure)" in s or s == "?":
        return None
    is_measure = " (measure)" in s
    s = s.replace(" (measure)", "")
    m = re.match(r"^\w+\((.*)\)(.*)$", s)
    if m:
        s = m.group(1) + m.group(2)
    for tname in sorted(model.tables, key=len, reverse=True):
        if not s.lower().startswith(tname.lower() + "."):
            continue
        rest = s[len(tname) + 1:]
        t = model.tables[tname]
        pool = ([(n, "measure") for t2 in model.tables.values() for n in t2.measures] if is_measure
                else [(n, "column") for n in t.columns] + [(n, "measure") for n in t.measures])
        for name, kind in sorted(pool, key=lambda p: len(p[0]), reverse=True):
            if rest.lower() == name.lower() or rest.lower().startswith(name.lower() + " "):
                return Ref(kind, tname if kind == "column" else "", name)
        # A hierarchy level, `Table.Hierarchy.Level`: levels are usually named after their column.
        if not is_measure and "." in rest:
            level = rest.split(" in [")[0].rsplit(".", 1)[1]
            if (col := _match(t.columns, level)):
                return Ref("column", tname, col)
        return None
    return None


def report_edges(b: _Builder, report_path: str, summary, model_path: str | None, model: ModelDef | None) -> None:
    """`report uses column|measure` for every field its visuals and filters bind."""
    from domain.report_bindings import REPORT_SUFFIX

    tree = b.g.tree
    rnode = f"report:{tree}/{report_path[:-len(REPORT_SUFFIX)]}"
    sref = f"{tree}:{report_path}"
    b.g.locations[rnode] = sref
    if model is None or model_path is None:
        b.gap(rnode, f"bound model not resolved in this tree ({summary.model or 'no definition.pbir'})", sref)
        return
    base, tid, cid, mid = _ids(tree, model_path)
    b.edge(rnode, f"fabric_model:{base}", "uses", sref)
    names = list(summary.filters)
    for page in summary.pages:
        names += page.filters
        for v in page.visuals:
            names += [f for vs in v.fields.values() for f in vs] + v.filters
    missed = set()
    for n in names:
        r = resolve_field(n, model)
        if r is None:
            if " (report measure)" not in n and n != "?":
                missed.add(n.split(" in [")[0])
            continue
        b.edge(rnode, cid(r.table, r.name) if r.kind == "column" else mid(r.name), "uses", sref)
    for n in sorted(missed):
        b.gap(rnode, f"report field not found in model: {n}", sref)


def generate_tree(tree: str, paths: list[str], read: Callable[[str], str | None], catalogs=()) -> TreeGraph:
    """Every model and report in one mirrored tree. `read(path)` returns a file's text, or None.
    `catalogs`: this deployment's Unity Catalog names, for native-SQL three-part table names."""
    from domain.report_bindings import definition_files, model_ref, report_folders, summarize_report

    b = _Builder(tree)
    models: dict[str, ModelDef] = {}
    for mpath, tmdl in sorted(model_folders(paths).items()):
        files = {p: text for p in tmdl if (text := read(p)) is not None}
        models[mpath] = parse_tmdl(files)
        model_edges(b, mpath, models[mpath], catalogs)
    reports = report_folders(paths)
    for rpath, rfiles in sorted(reports.items()):
        contents = {p: text for p in definition_files(rpath, rfiles) if (text := read(p)) is not None}
        try:
            summary = summarize_report(rpath, contents)
        except Exception:  # noqa: BLE001 — one malformed report must not sink the tree
            b.gap(f"report:{tree}/{rpath}", "report definition could not be parsed", f"{tree}:{rpath}")
            continue
        mpath = None
        ref = model_ref(contents.get(f"{rpath}/definition.pbir"))
        if ref and not ref.startswith("byConnection"):
            cand = posixpath.normpath(posixpath.join(rpath, ref.replace("\\", "/")))
            mpath = cand if cand in models else None
            if mpath is None:
                # A model moved without its reports' definition.pbir being updated: bind by folder
                # name when exactly one model in the tree has it, and keep the stale path listed.
                same = [m for m in models if m.rsplit("/", 1)[-1] == cand.rsplit("/", 1)[-1]]
                if len(same) == 1:
                    mpath = same[0]
                    b.gap(f"report:{tree}/{rpath[:-len('.Report')]}",
                          f"definition.pbir path is stale ({ref}); bound by folder name to {mpath}",
                          f"{tree}:{rpath}/definition.pbir")
        report_edges(b, rpath, summary, mpath, models.get(mpath) if mpath else None)
    g = b.g
    g.stats = {
        "models": len(models),
        "reports": len(reports),
        "measures": sum(len(t.measures) for m in models.values() for t in m.tables.values()),
        "edges": len(g.edges),
        "unresolved": len(g.unresolved),
    }
    return g
