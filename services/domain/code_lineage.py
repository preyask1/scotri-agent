"""Lineage edges generated from the mirror's Databricks notebooks and ADF pipelines: the upstream
half of the graph (ADF trigger → pipeline → notebook → table), to go with domain/model_lineage.py's
table → model → report half. Pure: no filesystem, no network; the caller hands in paths and text.

Edges (same vocabulary and data-flow orientation as the rest of the graph):
  notebook:<path> produces <schema>_table:<name>     a write: INSERT/MERGE/CREATE TABLE/saveAsTable ...
  notebook:<path> consumes <schema>_table:<name>     a read on any other line (FROM/JOIN/spark.table)
  notebook:<path> includes notebook:<path>           a `%run` (the included code runs inside the includer,
                                                     so a change to it reaches the includer)
  notebook:<path> triggers notebook:<path>           a `dbutils.notebook.run` of a literal path, or every
                                                     notebook in a folder a runner lists and runs
                                                     (NB_Gold_Runner's Gold_Notebooks/<src>/Wave<N>/)
  adf_pipeline:<name> feeds notebook:<path>          a DatabricksNotebook activity
  adf_pipeline:<name> triggers adf_pipeline:<name>   an ExecutePipeline activity
  adf_trigger:<name> triggers adf_pipeline:<name>    a trigger's pipeline list

Table names are matched lexically: a qualified name whose schema is written out (`<catalog>.gold.x`,
`{catalog}.gold.x`, `gold.x`) is kept, lower-cased, schema `gold` as `gold_table` and any other as
`<schema>_table`, the same ids model_lineage gives a Fabric partition's source. Names built
entirely from variables (`{schema}.{table}`), SQL Server names (`[EDW_I].[X]`) and unqualified
names are not resolvable here; a notebook with dynamic write targets is listed as unresolved. As
with table-usage, this says where to look, not proof: confirm a surprising edge by reading the line.
"""
from __future__ import annotations

import json
import posixpath
import re
from datetime import datetime, timezone
from typing import Callable

from domain.model_lineage import TreeGraph, Unresolved, _Builder, catalog_pattern
from domain.mirror_text import as_text

NOTEBOOK_EXTS = (".ipynb", ".py", ".sql")
# A Databricks Repos checkout path, `[/Workspace]/Repos/<user or folder>/<repo>/`: what follows it is
# a path relative to the repo root, i.e. a mirror path. Any repo name, so no client config is needed.
_REPO_ROOT = re.compile(r"/Repos/[^/\"'\s]+/[^/\"'\s]+(?:/|$)")


def _repo_relative(path: str) -> str | None:
    """The part of `path` after its last `/Repos/<x>/<repo>/`, or None when it isn't under a repo."""
    last = None
    for last in _REPO_ROOT.finditer(path):
        pass
    return None if last is None else path[last.end():]

# A qualified table name with a literal schema: [catalog.]schema.table, catalog possibly an f-string
# variable. Backticks allowed. Schema and table must be plain identifiers.
_QUALIFIED = re.compile(
    r"(?<![\w.\]`{@/])`?(?:(?:\{[A-Za-z_]\w*\}|[A-Za-z_]\w*)`?\.`?)?([A-Za-z_]\w*)`?\.`?([A-Za-z_]\w*)`?(?![\w`(])")
_DYNAMIC = re.compile(r"\{[A-Za-z_]\w*\}`?\.`?\{[A-Za-z_]\w*\}")
_WRITE_LEAD = re.compile(
    r"(?:saveAsTable|insertInto|\.table)\s*\(\s*f?[\"']|\bINSERT\s+(?:INTO|OVERWRITE)(?:\s+TABLE)?\s+"
    r"|\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?(?:TABLE|VIEW)(?:\s+IF\s+NOT\s+EXISTS)?\s+"
    r"|\bMERGE\s+INTO\s+|\bREPLACE\s+TABLE\s+|\bDELETE\s+FROM\s+|\bTRUNCATE\s+TABLE\s+|\bUPDATE\s+(?=\S+\s+SET\b)",
    re.IGNORECASE)
_WRITE_HINT = re.compile(r"\.write\b|\bwriteTo\b", re.IGNORECASE)
_COMMENT = re.compile(r"^\s*(?:#|--|//)")
_MAGIC = re.compile(r"^\s*#\s*MAGIC\s?")
_CELL_HEADER = re.compile(r"^# ── cell \d+ \[(\w+)\] ──")
# Schemas that are really something else when written as `x.y`: Python modules and objects.
_NOT_SCHEMAS = frozenset({
    "spark", "df", "dbutils", "pd", "np", "f", "os", "sys", "re", "json", "datetime", "time", "self",
    "sql", "functions", "types", "pyspark", "delta", "math", "random", "logging", "requests", "string",
    "display", "col", "lit", "path", "str", "int", "dt", "result", "row", "config", "conf", "args",
    "params", "data", "response", "file", "table", "tbl", "src", "tgt", "t", "s", "a", "b", "c", "x", "y",
    "dfs", "blob", "core", "windows", "net", "com", "www", "azuredatabricks",  # storage/host URLs
})
# `{gold_schema}` and friends: schema variables, resolved from string assignments (NB_Utils* holds
# the shared defaults every notebook %runs; a notebook's own assignment overrides them).
_SCHEMA_ASSIGN = re.compile(r"^\s*(\w*schema\w*)\s*=\s*[\"']([A-Za-z_]\w*)[\"']\s*$", re.IGNORECASE | re.MULTILINE)
_SCHEMA_VAR = re.compile(r"\{(\w*schema\w*)\}", re.IGNORECASE)
_SHARED_VARS = re.compile(r"(?:^|/)(?:NB_)?Utils?[^/]*\.(?:py|ipynb)$", re.IGNORECASE)
_TEMP_SCHEMAS = frozenset({"global_temp"})


_RUN = re.compile(r"^\s*(?:#\s*MAGIC\s+)?%run\s+(.+?)\s*$", re.MULTILINE)
_NOTEBOOK_RUN = re.compile(r"dbutils\.notebook\.run\(\s*(f?)([\"'])(.*?)\2")
_NOTEBOOK_RUN_VAR = re.compile(r"dbutils\.notebook\.run\(\s*[A-Za-z_]\w*\s*[,)]")  # a bare variable, not f"..."


def resolve_call(target: str, caller: str, notebooks: dict[str, str]) -> str | None:
    """A `%run`/`dbutils.notebook.run` target to a mirror path: relative to the calling notebook's
    folder, or absolute under the repo (`/Workspace/Repos/<x>/<repo>/...`).
    `notebooks` maps extension-less lower-case path -> path."""
    t = target.strip().strip("\"'").replace("\\", "/")
    rel = _repo_relative(t)
    if rel is not None:
        t = rel
    elif t.startswith("/"):
        return None  # a workspace path outside the repo (/Users/..., /Shared/...)
    else:
        t = posixpath.join(posixpath.dirname(caller), t)
    t = posixpath.normpath(t).strip("/")
    t = posixpath.splitext(t)[0] if t.lower().endswith(NOTEBOOK_EXTS) else t
    return notebooks.get(t.lower())


def notebook_calls(text: str, caller: str, notebooks: dict[str, str]) -> tuple[list, list, int]:
    """(includes, triggers, unresolved) for one notebook: [(callee path, line)] for each `%run` and
    each literal `dbutils.notebook.run`, plus how many calls name their target through a variable or
    an f-string, or point outside the repo."""
    includes, triggers, unresolved = [], [], 0
    line_of = lambda pos: text.count("\n", 0, pos) + 1  # noqa: E731
    for m in _RUN.finditer(text):
        target = resolve_call(m.group(1), caller, notebooks)
        if target:
            includes.append((target, line_of(m.start())))
        else:
            unresolved += 1
    for m in _NOTEBOOK_RUN.finditer(text):
        is_f, path = m.group(1), m.group(3)
        target = None if (is_f and "{" in path) else resolve_call(path, caller, notebooks)
        if target:
            triggers.append((target, line_of(m.start())))
        else:
            unresolved += 1
    unresolved += len(_NOTEBOOK_RUN_VAR.findall(text))
    return includes, triggers, unresolved


# `\r` allowed before the line end: .py notebooks in the mirror can have CRLF line endings.
_FOLDER_ASSIGN = re.compile(r"^[ \t]*\w*folder\w*[ \t]*=[ \t]*[\"']([^\"'{}\r\n]+)[\"'][ \t\r]*$", re.IGNORECASE | re.MULTILINE)
# `Gold_Notebooks_path = "/Workspace/Repos/<x>/<repo>/Gold_Notebooks/"` (NB_Utils)
_REPO_PATH_ASSIGN = re.compile(r"^[ \t]*(\w+)[ \t]*=[ \t]*[\"']([^\"'\r\n]*/Repos/[^\"'\r\n]*)[\"'][ \t\r]*$",
                               re.MULTILINE)
_BASE_PLUS_FOLDER = re.compile(r"\{(\w+)\}\{\w*folder\w*\}", re.IGNORECASE)
# the one-line form: os.listdir(f"{Gold_Notebooks_Sales_Path}POS/Wave1/")
_LISTDIR_LITERAL = re.compile(r"os\.listdir\(\s*f[\"']\{(\w+)\}([^\"'{}\n]+)[\"']")


def repo_path_vars(text: str) -> dict[str, str]:
    """Variables holding a repo path, as a lower-case path relative to the repo root ('' = root).
    The last uncommented assignment wins, as at run time."""
    found = {}
    for name, value in _REPO_PATH_ASSIGN.findall(text):
        rel = _repo_relative(value)
        if rel is not None:
            found[name] = rel.strip("/").lower()
    return found


def folder_runs(text: str, notebooks: dict[str, str], path_vars: dict[str, str] | None = None) -> list[tuple[str, int]]:
    """[(callee path, line)] for a runner that lists a folder and runs every notebook in it, like
    NB_Gold_Runner: `folder_name = 'STORES/Wave1/'`, `full_path = f"{Gold_Notebooks_path}{folder_name}"`,
    then `os.listdir(full_path)` and `dbutils.notebook.run(<variable>)`. Each folder assignment not
    commented out is placed under the base-path variable when `path_vars` resolves it (the runner's
    own assignment, else NB_Utils'), otherwise matched against the tree's folders by its trailing
    path, which must then be unique. Every notebook directly in that folder is a callee.
    `notebooks` maps extension-less lower-case path -> path."""
    if "os.listdir(" not in text or "dbutils.notebook.run(" not in text:
        return []
    by_dir: dict[str, list[str]] = {}
    for p in notebooks.values():
        by_dir.setdefault(posixpath.dirname(p).lower(), []).append(p)
    variables = {**(path_vars or {}), **repo_path_vars(text)}
    bases = {variables[v] for v in _BASE_PLUS_FOLDER.findall(text) if v in variables}
    out = []
    for m in _FOLDER_ASSIGN.finditer(text):
        rel = m.group(1).strip().strip("/").lower()
        if not rel:
            continue
        dirs = [d for d in (posixpath.join(b, rel) if b else rel for b in bases) if d in by_dir]
        if not bases:
            dirs = [d for d in by_dir if d == rel or d.endswith("/" + rel)]
        if len(dirs) == 1:
            line = text.count("\n", 0, m.start()) + 1
            out += [(p, line) for p in sorted(by_dir[dirs[0]])]
    for m in _LISTDIR_LITERAL.finditer(text):
        if m.group(1) not in variables or _COMMENT.match(text[text.rfind("\n", 0, m.start()) + 1:m.start()]):
            continue
        d = posixpath.join(variables[m.group(1)], m.group(2).strip().strip("/").lower()).strip("/")
        if d in by_dir:
            line = text.count("\n", 0, m.start()) + 1
            out += [(p, line) for p in sorted(by_dir[d])]
    return out


def table_node(schema: str, table: str) -> str:
    schema, table = schema.lower(), table.lower()
    return f"gold_table:{table}" if schema == "gold" else f"{schema}_table:{table}"


def _code_lines(text: str) -> list[tuple[int, str]]:
    """(line number, code) for every non-comment line; Databricks `# MAGIC` lines unwrapped."""
    out = []
    prose = False  # inside a markdown cell (notebook_to_text header) or a `# MAGIC %md` block
    for number, line in enumerate(text.splitlines(), 1):
        if (h := _CELL_HEADER.match(line)):
            prose = h.group(1) == "markdown"
            continue
        if line.startswith("# COMMAND ----------"):
            prose = False
            continue
        if _MAGIC.match(line) and _MAGIC.sub("", line, count=1).lstrip().startswith("%md"):
            prose = True
        if prose:
            continue
        if _MAGIC.match(line):
            line = _MAGIC.sub("", line, count=1)
            if line.lstrip().startswith("%"):
                continue  # %sql / %md / %run directive line
        if not line.strip() or _COMMENT.match(line):
            continue
        out.append((number, line))
    return out


def _names(segment: str, schemas: set[str] | None) -> list[tuple[str, str]]:
    out = []
    for schema, table in _QUALIFIED.findall(segment):
        s = schema.lower()
        if s in _NOT_SCHEMAS or s in _TEMP_SCHEMAS or (schemas is not None and s not in schemas):
            continue
        out.append((schema, table))
    return out


def schema_vars(text: str) -> dict[str, str]:
    """`gold_schema = 'gold'`-style assignments in one file (first wins), lower-cased names."""
    out: dict[str, str] = {}
    for name, value in _SCHEMA_ASSIGN.findall(text):
        out.setdefault(name.lower(), value)
    return out


def _substitute(line: str, variables: dict[str, str]) -> str:
    return _SCHEMA_VAR.sub(lambda m: variables.get(m.group(1).lower(), m.group(0)), line)


def notebook_tables(text: str, schemas: set[str], variables: dict[str, str] | None = None) -> tuple[dict, dict, int]:
    """(writes, reads, dynamic) for one notebook's text: {node: first line}, {node: first line}, and
    how many write statements name their target only through variables. `schemas` limits matches to
    real schema names (gathered from the tree's three-part names), which filters `obj.attr` noise."""
    writes: dict[str, int] = {}
    reads: dict[str, int] = {}
    dynamic = 0
    variables = {**(variables or {}), **schema_vars(text)}
    for number, line in _code_lines(text):
        line = _substitute(line, variables)
        m = _WRITE_LEAD.search(line)
        if m:
            after = line[m.end():]
            target = _names(after[:200], schemas)
            if target:
                writes.setdefault(table_node(*target[0]), number)
                rest = line[:m.start()] + after.replace(f"{target[0][0]}.{target[0][1]}", " ", 1)
            else:
                if _DYNAMIC.match(after.lstrip("`\"' ")):
                    dynamic += 1
                rest = line
        else:
            rest = line
        for schema, table in _names(rest, schemas):
            node = table_node(schema, table)
            if _WRITE_HINT.search(line) and not m:
                writes.setdefault(node, number)
            else:
                reads.setdefault(node, number)
    for node in writes:
        reads.pop(node, None)  # MERGE/UPDATE read their own target; that's not an upstream
    return writes, reads, dynamic


def known_schemas(texts: list[str], catalogs=()) -> set[str]:
    """Schema names seen in three-part `catalog.schema.table` references (literal or variable
    catalog) across a tree: the vocabulary two-part `schema.table` matches are checked against."""
    three = re.compile(rf"(?<![\w.@/])(?:\{{[A-Za-z_]\w*\}}|{catalog_pattern(catalogs)}|main|hive_metastore)"
                       r"`?\.`?([A-Za-z_]\w*)`?\.`?[A-Za-z_]\w*", re.IGNORECASE)
    found = {s.lower() for t in texts for s in three.findall(t)}
    return found - _NOT_SCHEMAS - _TEMP_SCHEMAS


def generate_databricks_tree(tree: str, paths: list[str], read: Callable[[str], str | None], catalogs=()) -> TreeGraph:
    b = _Builder(tree)
    texts = {}
    for p in paths:
        if p.endswith(NOTEBOOK_EXTS) and (raw := read(p)) is not None:
            texts[p] = as_text(p, raw)
    shared: dict[str, str] = {}
    shared_paths: dict[str, str] = {}
    for path in sorted(texts):
        if _SHARED_VARS.search(path):
            for k, v in schema_vars(texts[path]).items():
                shared.setdefault(k, v)
            for k, v in repo_path_vars(texts[path]).items():
                shared_paths.setdefault(k, v)
    schemas = known_schemas([_substitute(t, shared) for t in texts.values()], catalogs) | set(shared.values())
    notebooks = {posixpath.splitext(p)[0].lower(): p for p in texts}
    produced = calls = 0
    for path, text in sorted(texts.items()):
        node = f"notebook:{path}"
        writes, reads, dynamic = notebook_tables(text, schemas, shared)
        if writes or reads:
            b.g.locations[node] = f"{tree}:{path}:1"
        for table, line in writes.items():
            b.edge(node, table, "produces", f"{tree}:{path}:{line}")
        for table, line in reads.items():
            b.edge(node, table, "consumes", f"{tree}:{path}:{line}")
        includes, triggers, unresolved_calls = notebook_calls(text, path, notebooks)
        for callee, line in includes:
            b.edge(node, f"notebook:{callee}", "includes", f"{tree}:{path}:{line}")
        for callee, line in triggers:
            b.edge(node, f"notebook:{callee}", "triggers", f"{tree}:{path}:{line}")
        runs = [(c, line) for c, line in folder_runs(text, notebooks, shared_paths) if c != path]
        for callee, line in runs:
            b.edge(node, f"notebook:{callee}", "triggers", f"{tree}:{path}:{line}")
        if runs:  # the variable dbutils.notebook.run is the folder runner, now resolved
            unresolved_calls = max(0, unresolved_calls - len(_NOTEBOOK_RUN_VAR.findall(text)))
            triggers = triggers + runs
        calls += len(includes) + len(triggers)
        if includes or triggers:
            b.g.locations.setdefault(node, f"{tree}:{path}:1")
        if unresolved_calls:
            b.gap(node, f"{unresolved_calls} notebook call(s) (%run / dbutils.notebook.run) name a variable, "
                        f"an f-string or a path outside the repo", f"{tree}:{path}:1")
        produced += bool(writes)
        if dynamic:
            b.gap(node, f"{dynamic} write statement(s) name their target only through variables", f"{tree}:{path}:1")
    b.g.stats = {"notebooks": len(texts), "notebooks_writing": produced, "schemas": len(schemas),
                 "shared_schema_vars": shared, "notebook_calls": calls,
                 "edges": len(b.g.edges), "unresolved": len(b.g.unresolved)}
    return b.g


def resolve_notebook(path: str, notebooks: dict[str, str]) -> str | None:
    """An ADF notebookPath (`/Repos/<x>/<repo>/Gold_Notebooks/a/b`) to the mirror path
    (`Gold_Notebooks/a/b.ipynb`). `notebooks` maps extension-less lower-case path -> path."""
    p = _repo_relative(path.replace("\\", "/"))
    if p is None:
        return None
    return notebooks.get(posixpath.normpath(p).strip("/").lower())


def _activities(acts):
    for a in acts or []:
        yield a
        tp = a.get("typeProperties", {}) or {}
        for key in ("activities", "ifTrueActivities", "ifFalseActivities", "defaultActivities"):
            yield from _activities(tp.get(key))
        for case in tp.get("cases", []) or []:
            yield from _activities(case.get("activities"))


def trigger_inactive(props: dict, now: datetime | None = None) -> str | None:
    """Why a trigger starts nothing, or None: Stopped, or a schedule whose recurrence endTime has
    passed. ADF keeps showing an expired schedule as Started (seen on a dev pipeline whose schedule had ended)."""
    if props.get("runtimeState") not in (None, "Started"):
        return f"trigger is {props.get('runtimeState')}: it starts nothing"
    end = ((props.get("typeProperties") or {}).get("recurrence") or {}).get("endTime")
    if isinstance(end, str):
        try:
            ended = datetime.fromisoformat(end.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
        if ended < (now or datetime.now(timezone.utc).replace(tzinfo=None)):
            return f"trigger's schedule ended {end}: ADF still shows it Started, but it no longer fires"
    return None


def generate_adf_tree(tree: str, paths: list[str], read: Callable[[str], str | None],
                      notebook_paths: list[str], now: datetime | None = None) -> TreeGraph:
    """Pipelines and triggers in one ADF tree. `notebook_paths` are the Databricks tree's files, to
    resolve each DatabricksNotebook activity's notebookPath to a notebook node. A Stopped or expired
    trigger keeps its edges (it is still the configured starter) and is listed as a gap."""
    b = _Builder(tree)
    notebooks = {posixpath.splitext(p)[0].lower(): p for p in notebook_paths if p.endswith(NOTEBOOK_EXTS)}
    pipelines = triggers = 0
    for path in sorted(paths):
        if not path.endswith(".json") or not path.startswith(("pipeline/", "trigger/")):
            continue
        raw = read(path)
        try:
            doc = json.loads(raw or "")
        except ValueError:
            b.gap(f"adf_file:{path}", "JSON could not be parsed", f"{tree}:{path}")
            continue
        name = doc.get("name") or posixpath.splitext(posixpath.basename(path))[0]
        props = doc.get("properties", {}) or {}
        sref = f"{tree}:{path}"
        if path.startswith("pipeline/"):
            pipelines += 1
            node = f"adf_pipeline:{name}"
            b.g.locations[node] = sref
            for a in _activities(props.get("activities")):
                tp = a.get("typeProperties", {}) or {}
                if a.get("type") == "DatabricksNotebook":
                    nb = tp.get("notebookPath")
                    if not isinstance(nb, str):
                        b.gap(node, f"activity '{a.get('name')}': notebookPath is an expression", sref)
                        continue
                    target = resolve_notebook(nb, notebooks)
                    if target:
                        b.edge(node, f"notebook:{target}", "feeds", sref)
                    else:
                        b.gap(node, f"activity '{a.get('name')}': notebook not in the mirrored repo ({nb})", sref)
                elif a.get("type") == "ExecutePipeline":
                    ref = (tp.get("pipeline") or {}).get("referenceName")
                    if ref:
                        b.edge(node, f"adf_pipeline:{ref}", "triggers", sref)
        else:
            triggers += 1
            node = f"adf_trigger:{name}"
            b.g.locations[node] = sref
            if why := trigger_inactive(props, now):
                b.gap(node, why, sref)
            for pl in props.get("pipelines", []) or []:
                ref = (pl.get("pipelineReference") or {}).get("referenceName")
                if ref:
                    b.edge(node, f"adf_pipeline:{ref}", "triggers", sref)
            ref = ((props.get("pipeline") or {}).get("pipelineReference") or {}).get("referenceName")
            if ref:  # tumbling-window triggers name one pipeline
                b.edge(node, f"adf_pipeline:{ref}", "triggers", sref)
    b.g.stats = {"pipelines": pipelines, "triggers": triggers,
                 "edges": len(b.g.edges), "unresolved": len(b.g.unresolved)}
    return b.g


__all__ = ["generate_databricks_tree", "generate_adf_tree", "trigger_inactive", "notebook_tables", "notebook_calls", "folder_runs",
           "resolve_notebook",
           "table_node", "Unresolved"]
