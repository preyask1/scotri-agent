"""Generate the Data Platform Map: one self-contained HTML page for new joiners.

    services/.venv/Scripts/python.exe tools/platform_map/gen_platform_map.py [--out <file>]

Reads only local, already-derived state, never a live backend:
- the lineage index (`services/data/lineage.db`): models, reports, tables, notebooks and the edges
  between them, as generated from the Prod mirror after every sync;
- the repo registry (`services/config/repo_registry.yaml`): which Fabric trees are mirrored;
- the client config (`services/config/client.yaml`, rendered from the profile): the client's name,
  catalogs, which repo is which kind, legacy repos, Fabric workspaces and Entra tenants;
- the optional notes file (`docs/ai/client/platform-map-notes.json`): the curated prose a person
  writes (source systems, consuming apps, glossary, the main scheduled run, ...);
- the ADF Prod tree in the mirror: triggers, pipelines and which Databricks linked service each uses;
- KPI and measure definitions the index attached to model objects (`node_notes`);
- each Fabric report folder in the mirror: its pages, hidden flags and visual counts.

`template.html` beside this file holds the page, the same for every client. The generator puts the
data and the notes into it and warns about any pipeline or trigger the notes name that the ADF tree
no longer has, so the notes can't silently go stale. Every sync runs this once at the end
(`Container.after_mirror_sync`); the default output, `services/data/platform-map.html`, is
gitignored like the rest of `services/data/`.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]  # the workspace root
SERVICES = ROOT / "services"
TEMPLATE = Path(__file__).resolve().parent / "template.html"
DEFAULT_OUT = SERVICES / "data" / "platform-map.html"

DEFAULT_CLIENT = SERVICES / "config" / "client.yaml"
DEFAULT_NOTES = ROOT / "docs" / "ai" / "client" / "platform-map-notes.json"

# Table kinds that are Databricks tables (schema "<x>" -> kind "<x>_table"); `silver_table` is a
# generator artefact (column-like names), so it is left out.
SKIP_TABLE_KINDS = {"silver_table", "test_table", "dev_table"}
SKIP_TABLE_KIND_RE = re.compile(r"^(?:test|tmp|temp|scratch)_\w*_table$")


def load_client(path: Path) -> dict:
    """config/client.yaml, or {} when the deployment has none (engine defaults)."""
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_notes(path: Path) -> dict:
    """The optional curated notes; {} when absent. A broken file is an error, not silently empty."""
    if not path.is_file():
        return {}
    notes = load_json(path)
    if not isinstance(notes, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return notes


def repo_kind(client: dict, key: str) -> str:
    """databricks / adf / fabric for a repo key: client.yaml's repo_kinds, else the key's own name."""
    kinds = client.get("repo_kinds") or {}
    if key in kinds:
        return kinds[key]
    return key if key in ("databricks", "adf") else "fabric"


def workspace_map(client: dict) -> dict:
    """(repo_key, branch) -> Fabric workspace name, from the profile's fabric.workspaces."""
    return {(w.get("repo_key"), w.get("branch")): w.get("name", "") for w in client.get("fabric_workspaces") or []}


def tenants(client: dict) -> list[dict]:
    """The Entra tenants the profile names, one row per distinct id."""
    rows: dict[str, dict] = {}
    for key, holds in (("adf_tenant_id", "Azure Data Factory"), ("fabric_tenant_id", "Fabric and Power BI")):
        tid = client.get(key)
        if tid:
            row = rows.setdefault(tid, {"name": f"Tenant {len(rows) + 1}", "id": tid, "holds": []})
            row["holds"].append(holds)
    return [{**r, "holds": ", ".join(r["holds"])} for r in rows.values()]


def setting_path(name: str, default: Path) -> Path:
    """Same convention as Container.path_setting: a relative value resolves under services/."""
    value = os.environ.get(name)
    if not value:
        return default
    p = Path(value).expanduser()
    return p if p.is_absolute() else SERVICES / p


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def repo_rows(registry: dict, client: dict | None = None) -> list[dict]:
    """Every repo and branch the registry knows, for the page's 'where things live' table."""
    client = client or {}
    legacy, workspaces = set(client.get("legacy_repos") or []), workspace_map(client)
    rows = []
    for repo in registry["repos"]:
        key = repo["repo_key"]
        kind = repo_kind(client, key)
        layer = ("ADF" if kind == "adf" else "Databricks" if kind == "databricks"
                 else "Fabric (legacy)" if key in legacy else "Fabric")
        for env in repo.get("environments", []):
            org = env.get("ado_org") or repo.get("ado_org", "")
            project = env.get("ado_project") or repo.get("ado_project", "")
            name = env.get("ado_repo") or repo.get("ado_repo", "")
            rows.append({"layer": layer, "key": key, "org": org, "project": project, "repo": name,
                         "env": env.get("environment", ""), "branch": env.get("branch", ""),
                         "mirrored": bool(env.get("mirrored")), "catalog": env.get("catalog", ""),
                         "workspace": workspaces.get((key, env.get("branch", "")), ""),
                         "url": f"https://dev.azure.com/{org}/{project}/_git/{name}" if org and project and name else ""})
    return rows


def factory_rows(path: Path) -> list[dict]:
    """ADF factories from services/config/adf_factories.yaml: name, subscription, resource group."""
    if not path.is_file():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [{"name": f.get("factory_name", ""), "env": f.get("environment", ""),
             "subscription": f.get("subscription_id") or "", "resource_group": f.get("resource_group") or "",
             "default": bool(f.get("default"))} for f in doc.get("factories", [])]


def mirrored_trees(registry: dict, client: dict | None = None) -> tuple[list[dict], dict]:
    """Fabric trees (repo_key@branch) mirrored for Prod, plus the ADF Prod repo/branch."""
    client = client or {}
    legacy, workspaces = set(client.get("legacy_repos") or []), workspace_map(client)
    trees, adf = [], {}
    for repo in registry["repos"]:
        for env in repo.get("environments", []):
            if not env.get("mirrored"):
                continue
            ado_repo = env.get("ado_repo") or repo.get("ado_repo")
            kind = repo_kind(client, repo["repo_key"])
            if kind == "adf":
                adf = {"repo": ado_repo, "branch": env["branch"]}
            elif kind == "fabric":
                trees.append({"key": f"{repo['repo_key']}@{env['branch']}", "repo": ado_repo, "branch": env["branch"],
                              "workspace": workspaces.get((repo["repo_key"], env["branch"])) or "—",
                              "gen": "legacy" if repo["repo_key"] in legacy else "new"})
    return trees, adf


def _name(node_id: str) -> str:
    return node_id.split(":", 1)[1]


def _kind(node_id: str) -> str:
    return node_id.split(":", 1)[0]


def from_lineage(db: sqlite3.Connection) -> dict:
    q = lambda sql: db.execute(sql).fetchall()  # noqa: E731
    models = {}
    for (mid,) in q("select id from nodes where kind='fabric_model'"):
        tree, path = _name(mid).split("/", 1)
        models[mid] = {"id": mid, "tree": tree, "path": path, "area": path.split("/")[0] if "/" in path else "(root)",
                       "tables": 0, "measures": 0, "roles": [], "gold": set(), "reports": []}

    def model_of(node_id: str) -> str:
        return "fabric_model:" + _name(node_id).split("::")[0]

    for (dst,) in q("select dst from edges where rel='part_of' and dst like 'fabric_model:%'"):
        if dst in models:
            models[dst]["tables"] += 1
    for (mid,) in q("select id from nodes where kind='measure'"):
        if model_of(mid) in models:
            models[model_of(mid)]["measures"] += 1
    for (rid,) in q("select id from nodes where kind='rls_role'"):
        if model_of(rid) in models:
            models[model_of(rid)]["roles"].append(_name(rid).split("::", 1)[1])

    data_kinds = {k for (k,) in q("select distinct kind from nodes where kind like '%\\_table' escape '\\'")
                  if k != "fabric_table" and k not in SKIP_TABLE_KINDS and not SKIP_TABLE_KIND_RE.match(k)}
    consumers = collections.defaultdict(set)
    for (src, dst) in q("select src,dst from edges where rel='consumes' and src like 'fabric_table:%'"):
        if _kind(dst) in data_kinds and model_of(src) in models:
            models[model_of(src)]["gold"].add(dst)
            consumers[dst].add(model_of(src))
    reports = []
    for (src, dst) in q("select src,dst from edges where rel='uses' and src like 'report:%' and dst like 'fabric_model:%'"):
        tree, path = _name(src).split("/", 1)
        reports.append({"id": src, "tree": tree, "path": path, "model": dst})
        if dst in models:
            models[dst]["reports"].append(src)

    producers, readers = collections.defaultdict(set), collections.defaultdict(set)
    nb_in, nb_out = collections.defaultdict(set), collections.defaultdict(set)
    for (src, dst, rel) in q("select src,dst,rel from edges where src like 'notebook:%' and rel in ('produces','consumes')"):
        if _kind(dst) not in data_kinds:
            continue
        nb = _name(src)
        if rel == "produces":
            producers[dst].add(nb)
            nb_out[nb].add(dst)
        else:
            readers[dst].add(nb)
            nb_in[nb].add(dst)
    cols = collections.Counter(src for (src,) in q("select src from edges where rel='contains' and dst like 'gold_column:%'"))

    tables = [{"id": tid, "kind": k, "name": _name(tid), "cols": cols.get(tid, 0),
               "producers": sorted(producers[tid]), "nb_readers": len(readers[tid]),
               "models": sorted(consumers[tid])}
              for (tid, k) in q("select id,kind from nodes") if k in data_kinds
              and (producers[tid] or readers[tid] or consumers[tid])]
    tables.sort(key=lambda t: (-len(t["models"]), t["name"]))

    pl_children, pl_notebooks = collections.defaultdict(set), collections.defaultdict(set)
    for (src, dst) in q("select src,dst from edges where rel='triggers' and src like 'adf_pipeline:%' and dst like 'adf_pipeline:%'"):
        pl_children[_name(src)].add(_name(dst))
    for (src, dst) in q("select src,dst from edges where rel='feeds' and src like 'adf_pipeline:%' and dst like 'notebook:%'"):
        pl_notebooks[_name(src)].add(_name(dst))
    # a notebook that runs others: dbutils.notebook.run, including a runner's folders of notebooks
    nb_runs = collections.defaultdict(set)
    for (src, dst) in q("select src,dst from edges where rel='triggers' and src like 'notebook:%' and dst like 'notebook:%'"):
        nb_runs[_name(src)].add(_name(dst))

    built = dict(q("select key,value from meta")).get("last_build_at")
    return {
        "built": built,
        "models": [{**m, "gold": sorted(m["gold"]), "reports": sorted(m["reports"])} for m in models.values()],
        "reports": reports,
        "tables": tables,
        # Every notebook that reads or writes a table, so the page's lineage graph can walk through
        # readers too (a notebook that only reads is where an upstream trace usually ends).
        "notebooks": {nb: {"in": sorted(nb_in[nb]), "n_in": len(nb_in[nb]), "out": sorted(nb_out[nb]),
                           **({"runs": sorted(nb_runs[nb])} if nb_runs.get(nb) else {})}
                      for nb in sorted(set(nb_out) | set(nb_in) | set(nb_runs) | {c for cs in nb_runs.values() for c in cs})},
        "kpis": kpi_definitions(db, models),
        "_pl_children": pl_children,
        "_pl_notebooks": pl_notebooks,
    }


def kpi_definitions(db: sqlite3.Connection, models: dict) -> list[dict]:
    """KPI and measure definitions the index attached to model measures/columns: verified ones are
    the model's own description, unverified ones come from the report documents in docs/ai/client/reports/."""
    out = []
    for node_id, title, text, source, verified in db.execute(
            "select node_id,title,text,source_ref,verified from node_notes where kind='kpi_definition'"):
        model = "fabric_model:" + _name(node_id).split("::")[0]
        if model not in models:
            continue
        obj = _name(node_id).split("::", 1)[1] if "::" in node_id else ""
        out.append({"name": title.strip("* ") or obj, "kind": _kind(node_id), "object": obj, "model": model,
                    "text": text[:400], "verified": int(verified), "source": source})
    out.sort(key=lambda k: (k["name"].lower(), k["model"]))
    return out


def _schedule_text(props: dict) -> str:
    if props["type"] != "ScheduleTrigger":
        return "on file arrival"
    rec = props.get("typeProperties", {}).get("recurrence", {})
    sch = rec.get("schedule") or {}
    hours, minutes = sorted(sch.get("hours") or []), sch.get("minutes") or [0]
    times = ", ".join(f"{h:02d}:{m:02d}" for h in hours for m in sorted(minutes))
    days = sch.get("weekDays")
    if rec.get("frequency") == "Week":
        when = ", ".join(d[:3] for d in days) if days else "weekly"
    elif rec.get("frequency") in ("Day", None):
        when = "daily"
    else:
        when = f"every {rec.get('interval', 1)} {str(rec.get('frequency')).lower()}"
    return f"{when} {times}".strip()


def _activities(acts: list):
    for a in acts:
        yield a
        tp = a.get("typeProperties", {})
        for key in ("activities", "ifTrueActivities", "ifFalseActivities", "defaultActivities"):
            yield from _activities(tp.get(key, []))
        for case in tp.get("cases", []):
            yield from _activities(case.get("activities", []))


def from_adf(adf_root: Path, lineage: dict) -> dict:
    triggers = []
    for f in sorted((adf_root / "trigger").glob("*.json")):
        j = load_json(f)
        p = j["properties"]
        state = p.get("runtimeState")
        end = (p.get("typeProperties", {}).get("recurrence") or {}).get("endTime")
        if state == "Started" and isinstance(end, str) and end[:19] < datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"):
            state = "Expired"  # ADF still shows Started, but the schedule has ended
        triggers.append({"name": j["name"], "type": "schedule" if p["type"] == "ScheduleTrigger" else "file event",
                         "state": state, "ended": end if state == "Expired" else None, "when": _schedule_text(p),
                         "pipelines": [x["pipelineReference"]["referenceName"] for x in p.get("pipelines", [])]})

    dbx_ls = {}
    for f in sorted((adf_root / "linkedService").glob("*.json")):
        j = load_json(f)
        if j["properties"].get("type") == "AzureDatabricks":
            dbx_ls[j["name"]] = j["properties"].get("description", "")
    ls_use = collections.Counter()
    pipelines = []
    for f in sorted((adf_root / "pipeline").glob("*.json")):
        j = load_json(f)
        name, props = j["name"], j["properties"]
        used = {a.get("linkedServiceName", {}).get("referenceName") for a in _activities(props.get("activities", []))}
        for ls in used & dbx_ls.keys():
            ls_use[ls] += 1
        pipelines.append({"name": name, "folder": (props.get("folder") or {}).get("name", ""),
                          "children": sorted(lineage["_pl_children"].get(name, ())),
                          "notebooks": sorted(lineage["_pl_notebooks"].get(name, ()))})
    compute = [{"ls": ls, "desc": desc, "pipelines": ls_use.get(ls, 0)}
               for ls, desc in sorted(dbx_ls.items(), key=lambda kv: -ls_use.get(kv[0], 0))]
    return {"triggers": triggers, "pipelines": pipelines, "compute": compute}


def report_pages(report_dir: Path) -> tuple[str | None, list]:
    """(format, [[page name, hidden 0/1, visual count], ...]) for one .Report folder, PBIR or legacy."""
    pages_dir = report_dir / "definition" / "pages"
    page_files = list(pages_dir.glob("*/page.json"))
    if page_files:
        by_name = {}
        for f in page_files:
            j = load_json(f)
            by_name[j.get("name") or f.parent.name] = [j.get("displayName"), int(j.get("visibility") == "HiddenInViewMode"),
                                                       len(list(f.parent.glob("visuals/*/visual.json")))]
        order = []
        if (pages_dir / "pages.json").is_file():
            order = load_json(pages_dir / "pages.json").get("pageOrder") or []
        order += [k for k in by_name if k not in order]
        return "PBIR", [by_name[k] for k in order if k in by_name]
    legacy = report_dir / "report.json"
    if legacy.is_file():
        pages = []
        for s in load_json(legacy).get("sections", []):
            try:
                hidden = int(json.loads(s.get("config") or "{}").get("visibility") == 1)
            except ValueError:
                hidden = 0
            pages.append([s.get("displayName"), hidden, len(s.get("visualContainers", []))])
        return "legacy", pages
    return None, []


def cited_names(notes: dict) -> tuple[set, set]:
    """(pipelines, triggers) the notes name: every `pipelines`/`triggers` list, at any depth, plus
    start_here.trigger."""
    pipes, trigs = set(), set()

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "pipelines" and isinstance(v, list):
                    pipes.update(str(n) for n in v)
                elif k == "triggers" and isinstance(v, list):
                    trigs.update(str(n) for n in v)
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(notes)
    start = (notes.get("start_here") or {}).get("trigger")
    if start:
        trigs.add(str(start))
    return pipes, trigs


def stale_names(notes: dict, pipelines: set, triggers: set) -> list[str]:
    """Pipeline/trigger names the notes cite that the ADF tree doesn't have."""
    cited_p, cited_t = cited_names(notes)
    return sorted(f"pipeline {n}" for n in cited_p - pipelines) + sorted(f"trigger {n}" for n in cited_t - triggers)


def build(lineage_db: Path, mirror_root: Path, registry_path: Path, template_path: Path = TEMPLATE,
          client_path: Path | None = None, notes_path: Path | None = None) -> tuple[str, dict]:
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    client = load_client(client_path or registry_path.parent / "client.yaml")
    notes = load_notes(notes_path or DEFAULT_NOTES)
    trees, adf = mirrored_trees(registry, client)
    active = mirror_root / "mirror-active"
    db = sqlite3.connect(f"file:{lineage_db.as_posix()}?mode=ro", uri=True)
    try:
        lin = from_lineage(db)
    finally:
        db.close()
    adf_data = from_adf(active / adf["repo"] / adf["branch"], lin) if adf else {"triggers": [], "pipelines": [], "compute": []}

    roots = {t["key"]: active / t["repo"] / t["branch"] for t in trees}
    for r in lin["reports"]:
        root = roots.get(r["tree"])
        r["fmt"], r["pages"] = report_pages(root / f"{r['path']}.Report") if root else (None, [])
    for t in trees:
        ms = [m for m in lin["models"] if m["tree"] == t["key"]]
        t.update(models=len(ms), reports=sum(1 for r in lin["reports"] if r["tree"] == t["key"]),
                 measures=sum(m["measures"] for m in ms))

    data = {"built": lin["built"] or datetime.now(timezone.utc).isoformat(), "trees": trees,
            "repos": repo_rows(registry, client), "factories": factory_rows(registry_path.parent / "adf_factories.yaml"),
            "tenants": tenants(client), "catalogs": client.get("catalogs") or {}, "notes": notes,
            "client": {"client_name": client.get("client_name") or "", "display_name": client.get("display_name") or "Scotri"},
            **{k: v for k, v in lin.items() if not k.startswith("_") and k != "built"}, **adf_data}
    template = template_path.read_text(encoding="utf-8")
    warnings = stale_names(notes, {p["name"] for p in data["pipelines"]}, {t["name"] for t in data["triggers"]})
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    html = template.replace("/*DATA*/", payload, 1)
    stats = {"trees": len(trees), "models": len(data["models"]), "reports": len(data["reports"]),
             "tables": len(data["tables"]), "notebooks": len(data["notebooks"]),
             "pipelines": len(data["pipelines"]), "triggers": len(data["triggers"]), "kpis": len(data["kpis"]),
             "bytes": len(html.encode("utf-8")), "stale_names": warnings}
    return html, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--lineage-db", type=Path, default=setting_path("LINEAGE_DB_PATH", SERVICES / "data" / "lineage.db"))
    ap.add_argument("--mirror-root", type=Path, default=setting_path("MIRROR_ROOT", SERVICES / "mirror"))
    ap.add_argument("--registry", type=Path, default=SERVICES / "config" / "repo_registry.yaml")
    ap.add_argument("--client", type=Path, default=DEFAULT_CLIENT, help="config/client.yaml (rendered from the profile).")
    ap.add_argument("--notes", type=Path, default=DEFAULT_NOTES, help="The curated notes JSON (optional).")
    args = ap.parse_args(argv)
    if not args.lineage_db.is_file():
        print(json.dumps({"ok": False, "message": f"lineage index not found: {args.lineage_db}"}))
        return 1
    html, stats = build(args.lineage_db, args.mirror_root, args.registry, client_path=args.client, notes_path=args.notes)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, args.out)  # a reader never sees a half-written page
    print(json.dumps({"ok": True, "out": str(args.out), **stats}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
