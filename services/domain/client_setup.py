"""Renders a validated client profile (domain/client_profile.py) into the files a deployment needs.

Two kinds of output:
- GENERATED files are rebuilt on every `python -m app.setup render` and start with a "generated"
  header: the services/config/*.yaml registries and the docs/ai/client/ facts docs. Never hand-edit
  them; edit client/profile.yaml and re-render.
- SKELETON docs (architecture, conventions, lineage) are domain knowledge a person writes. Render
  creates them once, with TODO sections, and never overwrites them afterwards.

Pure: returns {relative path: content}; app/setup.py does the file I/O. Paths are relative to the
workspace root.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template

from domain.client_config import COMPONENT_LABELS, OPTIONAL_COMPONENTS
from domain.client_profile import interpreter_path

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "setup_templates"

YAML_HEADER = ("# GENERATED from client/profile.yaml by `python -m app.setup render` (run from services/).\n"
               "# Do not edit: change the profile and re-render. See SETUP.md.\n")
MD_HEADER = ("<!-- GENERATED from client/profile.yaml by `python -m app.setup render`. Do not edit: change "
             "the profile and re-render (SETUP.md). -->")

CONFIG_DIR = "services/config"
CLIENT_DOCS = "docs/ai/client"


def _yaml(data: dict) -> str:
    return YAML_HEADER + "\n" + _block_yaml(data) + "\n"


def _scalar(value) -> str:
    """A YAML scalar. A string is written plain when it can't be misread (a bool, a number, a
    comment, a mapping, a date); otherwise single-quoted as PyYAML writes it, or as a JSON string
    (a valid YAML double-quoted scalar) when it holds a line break or a control character."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if (_PLAIN.fullmatch(text) and text.lower() not in _RESERVED and not _NUMBERLIKE.fullmatch(text)
            and not _DATELIKE.fullmatch(text)):
        return text
    if "\n" in text or not text.isprintable():
        return json.dumps(text, ensure_ascii=False)
    return "'" + text.replace("'", "''") + "'"


_PLAIN = re.compile(r"[A-Za-z0-9_/][A-Za-z0-9_./@()-]*(?: [A-Za-z0-9_./@()-]+)*")
_DATELIKE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}.*|\d+(:\d+)+.*")
_RESERVED = {"y", "n", "yes", "no", "true", "false", "on", "off", "null", "~"}
_NUMBERLIKE = re.compile(r"[-+]?(\d[\d_]*)?\.?\d*([eE][-+]?\d+)?|0[xob][0-9a-fA-F_]+")


def _block_yaml(data, indent: int = 0) -> str:
    """Block-style YAML for the dicts, lists and scalars a profile renders to. Stdlib only, so the
    domain layer stays free of third-party imports (tests/test_architecture.py); the output is read
    back with PyYAML in tests/test_client_setup.py."""
    pad = " " * indent
    lines: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{_scalar(key) if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', str(key)) else key}:")
                # a list sits at its key's indent, a mapping one level in (PyYAML's block style)
                lines.append(_block_yaml(value, indent if isinstance(value, list) else indent + 2))
            else:
                empty = "{}" if isinstance(value, dict) else "[]" if isinstance(value, list) else _scalar(value)
                name = key if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", str(key)) else _scalar(key)
                lines.append(f"{pad}{name}: {empty}")
    else:
        for item in data:
            if isinstance(item, (dict, list)) and item:
                nested = _block_yaml(item, indent + 2)
                lines.append(f"{pad}- {nested[indent + 2:]}")
            else:
                empty = "{}" if isinstance(item, dict) else "[]" if isinstance(item, list) else _scalar(item)
                lines.append(f"{pad}- {empty}")
    return "\n".join(lines)


def _md(description: str, body: str) -> str:
    return f'---\ndescription: "{description}"\n---\n{MD_HEADER}\n\n{body.strip()}\n'


def _repos(profile: dict, kind: str | None = None) -> list[dict]:
    return [r for r in profile["repos"] if kind is None or r["kind"] == kind]


def _ado_repo(repo: dict, env: dict) -> str:
    return env.get("ado_repo") or repo.get("ado_repo") or ""


# --- services/config ------------------------------------------------------------------------

def render_repo_registry(profile: dict) -> str:
    repos = []
    for r in profile["repos"]:
        entry: dict = {"repo_key": r["repo_key"]}
        envs_have_repo = any(e.get("ado_repo") for e in r["environments"])
        if r.get("ado_repo") and not envs_have_repo:
            entry["ado_repo"] = r["ado_repo"]
        entry["ado_org"] = r["ado_org"]
        entry["ado_project"] = r["ado_project"]
        if r.get("status"):
            entry["status"] = r["status"]
        environments = []
        for e in r["environments"]:
            env: dict = {"environment": e["environment"]}
            if envs_have_repo:
                env["ado_repo"] = _ado_repo(r, e)
            env["branch"] = e["branch"]
            env["mirrored"] = e["mirrored"]
            if e.get("catalog"):
                env["catalog"] = e["catalog"]
            environments.append(env)
        entry["environments"] = environments
        repos.append(entry)
    return _yaml({"repos": repos})


def render_adf_factories(profile: dict) -> str:
    factories = []
    for f in (profile["adf"] or {}).get("factories", []):
        entry = {"environment": f["environment"], "subscription_id": f["subscription_id"],
                 "resource_group": f["resource_group"], "factory_name": f["factory_name"]}
        if f["default"]:
            entry["default"] = True
        factories.append(entry)
    return _yaml({"factories": factories})


def render_model_registry(profile: dict) -> str:
    return _yaml({"default": {"vendor": "anthropic", "model": profile["model"],
                              "secret_name": "ANTHROPIC_API_KEY"}})


def render_client_config(profile: dict) -> str:
    c = profile["client"]
    return _yaml({
        "client_name": c["name"],
        "display_name": c["display_name"],
        "logo": c["logo"],
        "components": dict(profile["components"]),
        "catalogs": dict(profile["databricks"]["catalogs"]),
        "jira_projects": list(profile["jira"]["projects"]),
    })


# --- docs/ai/client --------------------------------------------------------------------------

def _branch_list(repo: dict, mirrored: bool) -> str:
    items = [f"`{e['branch']}` ({e['environment']}{', ' + _ado_repo(repo, e) if e.get('ado_repo') else ''})"
             for e in repo["environments"] if e["mirrored"] == mirrored]
    return ", ".join(items) or "—"


def render_client_md(profile: dict) -> str:
    c, dbx, jira = profile["client"], profile["databricks"], profile["jira"]
    interp = interpreter_path(profile)
    cat = dbx["catalogs"]
    project = jira["projects"][0]
    lines = [
        f"# Client facts: {c['name']}",
        "",
        "The deployment-specific values the operating rules and skills refer to. Generated from",
        "`client/profile.yaml`; the domain docs next to this file are written by people.",
        "",
        "## Your name",
        "",
        f"In everything a user reads (answers, deliverables, Jira comments), call yourself **{c['display_name']}**.",
        "",
        "## Running commands",
        "",
        f"The interpreter is `services/{interp}`, so every command is:",
        "",
        "```bash",
        f'cd "<this workspace root>/services" && "<this workspace root>/services/{interp}" -m app.cli <command> [args...]',
        "```",
        "",
        "## Components",
        "",
        "| Component | In this deployment |",
        "|---|---|",
        "| Databricks | on |",
        "| Jira | on |",
    ]
    for comp in OPTIONAL_COMPONENTS:
        lines.append(f"| {COMPONENT_LABELS[comp]} | {'on' if profile['components'][comp] else '**off**'} |")
    off = [COMPONENT_LABELS[x] for x in OPTIONAL_COMPONENTS if not profile["components"][x]]
    if off:
        lines += ["", f"{' and '.join(off)} {'is' if len(off) == 1 else 'are'} not part of this platform. Commands "
                      "for it answer `not enabled for this deployment`: record that as out of scope, never as an "
                      "outage, and don't look for that layer in an impact analysis."]
    lines += [
        "",
        "## Databricks",
        "",
        f"- Prod catalog: `{cat['prod']}`, the default for every query.",
        (f"- Dev catalog: `{cat['dev']}`, only when Dev is named (presence check first)."
         if cat.get("dev") else "- No dev catalog: a Dev question can't be answered from Databricks here."),
        (f"- UAT catalog: `{cat['uat']}`." if cat.get("uat")
         else "- No UAT catalog: a UAT question about Databricks data is a dev-catalog question."),
        f"- Schemas: {', '.join(f'`{s}`' for s in dbx['schemas'])}.",
        f"- Query workspace: `{dbx['host']}`.",
        f"- In the skills and examples, `<prod_catalog>` means `{cat['prod']}`"
        + (f" and `<dev_catalog>` means `{cat['dev']}`." if cat.get("dev") else "."),
    ]
    sec = dbx.get("secondary_workspace")
    if sec:
        lines += [f"- Secondary workspace `{sec['host']}`" + (f" ({sec['purpose']})" if sec["purpose"] else "")
                  + ": use it **only** from `bug-investigation`'s pipeline-failure chain trace, when that trace "
                  "lands on a job hosted there. Never from `impact-analysis`, never for Story work. Same "
                  "read-only restrictions."]
    else:
        lines += ["- There is no secondary Databricks workspace: ignore any rule about one."]
    lines += [
        "",
        "## Jira",
        "",
        f"- Site: {jira['site_url']}",
        f"- Projects: {', '.join(f'`{p}`' for p in jira['projects'])} (primary: `{project}`). Ticket keys look "
        f"like `{project}-123`.",
        "",
        "## Repos (`--repo-key` values)",
        "",
        "| `--repo-key` | Kind | ADO repo | Mirrored (Prod) | Live read only | Note |",
        "|---|---|---|---|---|---|",
    ]
    for r in profile["repos"]:
        lines.append(f"| `{r['repo_key']}` | {r['kind']} | {r.get('ado_repo') or '(per environment)'} | "
                     f"{_branch_list(r, True)} | {_branch_list(r, False)} | {r['note']} |")
    multi = [r for r in profile["repos"] if sum(1 for e in r["environments"] if e["mirrored"]) > 1]
    for r in multi:
        default, *others = [e["branch"] for e in r["environments"] if e["mirrored"]]
        lines += ["", f"`{r['repo_key']}` has more than one mirrored branch: `{default}` is the default; pass "
                      + ", ".join(f"`--branch {b}`" for b in others) + " for the other."]
    lines += ["", "Repo keys use underscores. Full read rules: [`repo-registry.md`](repo-registry.md)."]
    if profile["fabric"] and profile["fabric"]["workspaces"]:
        lines += ["", "## Fabric workspaces", "", "| Workspace | Synced from |", "|---|---|"]
        for ws in profile["fabric"]["workspaces"]:
            src = f"`{ws.get('repo_key', '')}` / `{ws.get('branch', '')}`" if ws.get("repo_key") else "not git-synced"
            lines.append(f"| `{ws['name']}` | {src} |")
        lines += ["", "Pass these names to `dax-execute`/`refresh-history --workspace`; never a `WorkspaceName` "
                      "parameter found in TMDL."]
    if profile["adf"]:
        names = [f["factory_name"] for f in profile["adf"]["factories"]]
        default = next((f["factory_name"] for f in profile["adf"]["factories"] if f["default"]), names[0])
        lines += ["", "## ADF factories", "",
                  f"`--factory` takes one of {', '.join(f'`{n}`' for n in names)}; `{default}` is the default. "
                  "A pipeline run lives in one factory: query the factory that owns the pipeline."]
    lines += ["", "## Domain docs", "",
              "Written by people, next to this file: "
              + ", ".join(f"[`{n}`]({n})" for n in _skeleton_names(profile)) + "."]
    return _md(f"Client facts for {c['name']}: display name, interpreter, components, catalogs, Jira projects, "
               "repo keys, workspaces and factories. Generated from client/profile.yaml.", "\n".join(lines))


def render_repo_registry_md(profile: dict) -> str:
    body = Template((TEMPLATES_DIR / "repo-registry-intro.md").read_text(encoding="utf-8")).substitute(
        name=profile["client"]["name"])
    parts = [body.strip()]
    for kind, title in (("databricks", "Databricks"), ("adf", "Azure Data Factory"), ("fabric", "Power BI / Fabric")):
        repos = _repos(profile, kind)
        if not repos:
            continue
        parts += ["", f"## {title}", "",
                  "| Repo (`--repo-key`) | ADO org / project | Environment | ADO repo | Branch | Mirrored | Catalog |",
                  "|---|---|---|---|---|---|---|"]
        for r in repos:
            for e in r["environments"]:
                parts.append(f"| `{r['repo_key']}` | `{r['ado_org']}` / `{r['ado_project']}` | {e['environment']} | "
                             f"`{_ado_repo(r, e)}` | `{e['branch']}` | {'yes' if e['mirrored'] else 'no — live read only'} | "
                             f"{'`' + e['catalog'] + '`' if e.get('catalog') else '—'} |")
        notes = [f"- `{r['repo_key']}`: {r['note']}" for r in repos if r["note"]]
        if notes:
            parts += [""] + notes
    missing = [e for e in ("prod", "dev", "uat") if not any(
        env["environment"] == e for r in profile["repos"] for env in r["environments"])]
    if missing:
        parts += ["", "No repo has a " + " or ".join(f"`{m}`" for m in missing) + " environment: a question about "
                  "it has no tree to read. Say so rather than falling back to another branch."]
    return _md(f"The single source of truth for which repos/branches exist in {profile['client']['name']}'s platform, "
               "per environment, and how this agent reads each one. Generated from client/profile.yaml.",
               "\n".join(parts))


def render_repo_map_md(profile: dict) -> str:
    lines = ["# Repo map — workspaces, tenants and the doc index", "",
             "Which repos and branches exist is in [`repo-registry.md`](repo-registry.md). This file covers how "
             "they connect to the rest of the platform."]
    if profile["fabric"]:
        lines += ["", "## Fabric Git integration — workspace → repo/branch", ""]
        if profile["fabric"]["workspaces"]:
            lines += ["| Fabric workspace | Repo | Branch |", "|---|---|---|"]
            for ws in profile["fabric"]["workspaces"]:
                lines.append(f"| `{ws['name']}` | `{ws.get('repo_key') or '—'}` | `{ws.get('branch') or '—'}` |")
            lines += ["", "A workspace with no Git integration cannot be read from the mirror at all; reaching it "
                          "would need the Fabric API directly."]
        else:
            lines.append("No Fabric workspaces are listed in the profile yet.")
    tenants = ["| Plane | Used for |", "|---|---|",
               "| Azure DevOps | ADO Git REST, used only by the sync service to refresh the mirror (read-only PAT) |",
               "| Databricks | Databricks SDK, service-principal M2M OAuth, via the query governor |"]
    if profile["adf"]:
        tenants.append("| Azure Data Factory | ARM run-history reads by the `ADF_*` app registration "
                       "(`ADF_TENANT_ID`), via a federated credential |")
    if profile["fabric"]:
        tenants.append("| Power BI / Fabric | Fabric REST (`dax-debug`, `refresh-history`) by the Fabric service "
                       "principal (`FABRIC_TENANT_ID`) |")
    lines += ["", "## Entra tenants", "",
              "The data plane and the BI plane may sit in different Entra tenants; each credential names its own.",
              "", *tenants, "",
              "🔴 **A wrong-tenant token returns HTTP 203 with an HTML sign-in page, not a 401.** Treat an HTML "
              "response as a wrong tenant, never as an empty result."]
    lines += ["", "## Which doc covers what", "", "| Topic | Doc |", "|---|---|",
              "| Client facts (catalogs, projects, repo keys) | [`client.md`](client.md) |",
              "| Repo/branch/environment registry | [`repo-registry.md`](repo-registry.md) |"]
    titles = {"databricks-architecture.md": "Databricks architecture", "fabric-architecture.md": "Fabric architecture",
              "fabric-conventions.md": "Fabric conventions", "lineage-map.md": "Cross-repo lineage"}
    for name in _skeleton_names(profile):
        if name in titles:
            lines.append(f"| {titles[name]} | [`{name}`]({name}) |")
    lines += ["| Tool access (mechanics) | [`../fabric-tool-access.md`](../fabric-tool-access.md) |",
              "| Jira access | [`../jira-access.md`](../jira-access.md) |"]
    return _md("Fabric Git integration, Entra tenants and the doc index for this deployment. Generated from "
               "client/profile.yaml.", "\n".join(lines))


# --- skeletons -------------------------------------------------------------------------------

def _skeleton_names(profile: dict) -> list[str]:
    names = ["databricks-architecture.md"]
    if profile["components"]["fabric"]:
        names += ["fabric-architecture.md", "fabric-conventions.md"]
    return names + ["lineage-map.md", "lineage-graph.yaml"]


def render_skeletons(profile: dict) -> dict[str, str]:
    """Create-only docs, filled with the profile's names and TODO sections for people to complete."""
    dbx = profile["databricks"]
    values = {
        "name": profile["client"]["name"],
        "prod_catalog": dbx["catalogs"]["prod"],
        "dev_catalog": dbx["catalogs"].get("dev", "(none)"),
        "schemas": ", ".join(f"`{s}`" for s in dbx["schemas"]),
        "databricks_repo": next(r["repo_key"] for r in profile["repos"] if r["kind"] == "databricks"),
        "fabric_repos": ", ".join(f"`{r['repo_key']}`" for r in _repos(profile, "fabric")) or "(none)",
    }
    out = {}
    for name in _skeleton_names(profile):
        template = Template((TEMPLATES_DIR / name).read_text(encoding="utf-8"))
        out[f"{CLIENT_DOCS}/{name}"] = template.substitute(values)
    return out


def render_generated(profile: dict) -> dict[str, str]:
    """Every generated file, rebuilt on each render."""
    return {
        f"{CONFIG_DIR}/repo_registry.yaml": render_repo_registry(profile),
        f"{CONFIG_DIR}/adf_factories.yaml": render_adf_factories(profile),
        f"{CONFIG_DIR}/model_registry.yaml": render_model_registry(profile),
        f"{CONFIG_DIR}/client.yaml": render_client_config(profile),
        f"{CLIENT_DOCS}/client.md": render_client_md(profile),
        f"{CLIENT_DOCS}/repo-registry.md": render_repo_registry_md(profile),
        f"{CLIENT_DOCS}/repo-map.md": render_repo_map_md(profile),
    }


# --- services/.env ---------------------------------------------------------------------------

def env_from_profile(profile: dict) -> dict[str, str]:
    """Non-secret settings the profile already knows, written into services/.env by `setup secrets`."""
    values = {
        "DATABRICKS_HOST": f"https://{profile['databricks']['host']}",
        "DATABRICKS_SQL_WAREHOUSE_ID": profile["databricks"]["warehouse_id"],
        "JIRA_BASE_URL": profile["jira"]["site_url"],
    }
    if profile["fabric"]:
        values["FABRIC_TENANT_ID"] = profile["fabric"]["tenant_id"]
    if profile["adf"]:
        values["ADF_TENANT_ID"] = profile["adf"]["tenant_id"]
        values["ADF_CLIENT_ID"] = profile["adf"]["client_id"]
    return {k: v for k, v in values.items() if v}


def ado_pat_env_name(org: str) -> str:
    """The per-org PAT variable adapters/ado_auth.py looks for first: ADO_PAT_READONLY_<ORG>, with every
    run of non-alphanumerics turned into "_" and upper-cased."""
    return "ADO_PAT_READONLY_" + re.sub(r"[^A-Za-z0-9]+", "_", org).upper()


def ado_pat_names(profile: dict) -> list[str]:
    """ADO_PAT_READONLY covers the first org; every other org gets its own ADO_PAT_READONLY_<ORG>."""
    orgs = list(dict.fromkeys(r["ado_org"] for r in profile["repos"]))
    return ["ADO_PAT_READONLY"] + [ado_pat_env_name(o) for o in orgs[1:]]


def credential_prompts(profile: dict) -> list[tuple[str, str, bool]]:
    """(env var, what to paste, is_secret) for every credential the enabled components need."""
    prompts = [
        ("DATABRICKS_CLIENT_ID", "Databricks service principal application (client) id", False),
        ("DATABRICKS_CLIENT_SECRET", "Databricks service principal OAuth secret", True),
    ]
    if not profile["databricks"]["warehouse_id"]:
        prompts.append(("DATABRICKS_SQL_WAREHOUSE_ID", "SQL warehouse id the agent queries through", False))
    orgs = list(dict.fromkeys(r["ado_org"] for r in profile["repos"]))
    for org, name in zip(orgs, ado_pat_names(profile)):
        prompts.append((name, f"Azure DevOps PAT with Code (Read) only, for org '{org}'", True))
    prompts += [("JIRA_CLOUD_ID", "Jira cloud id (setup fetches it from the site when it can)", False),
                ("JIRA_EMAIL", "Jira service-account email", False),
                ("JIRA_API_TOKEN", "Jira scoped API token", True)]
    if profile["fabric"]:
        if not profile["fabric"]["tenant_id"]:
            prompts.append(("FABRIC_TENANT_ID", "Entra tenant id Power BI/Fabric lives in", False))
        prompts += [("FABRIC_SP_CLIENT_ID", "Fabric/Power BI service principal application id", False),
                    ("FABRIC_SP_CLIENT_SECRET", "Fabric/Power BI service principal secret", True)]
    if profile["adf"]:
        if not profile["adf"]["tenant_id"]:
            prompts.append(("ADF_TENANT_ID", "Entra tenant id the data factories live in", False))
        if not profile["adf"]["client_id"]:
            prompts.append(("ADF_CLIENT_ID", "ADF reader app registration client id", False))
        prompts += [("ADF_MANAGED_IDENTITY_OBJECT_ID", "object id of the host's user-assigned managed identity "
                                                        "(blank on a laptop)", False)]
    prompts += [("ANTHROPIC_API_KEY", "Anthropic API key", True),
                ("AZURE_AD_TENANT_ID", "Entra tenant that issues tokens for this API (blank for dev auth)", False),
                ("AZURE_AD_API_AUDIENCE", "this API's app registration client id / App ID URI (blank for dev auth)",
                 False)]
    return prompts


def update_env_text(text: str, values: dict[str, str]) -> str:
    """Sets each KEY=value in a .env-style text, replacing an existing assignment line (comment kept
    on the line above, never inline: python-dotenv would read it as part of the value) or appending
    the ones the text doesn't have."""
    lines = text.splitlines()
    remaining = dict(values)
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={_quote(remaining.pop(key))}"
    if remaining:
        lines += ["", "# Added by `python -m app.setup secrets`"]
        lines += [f"{k}={_quote(v)}" for k, v in remaining.items()]
    return "\n".join(lines) + "\n"


def _quote(value: str) -> str:
    if value == "" or any(ch in value for ch in " #\"'"):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value
