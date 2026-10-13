"""Validates client/profile.yaml — the one file that describes a deployment (SETUP.md).

The profile holds identifiers and choices only, never secrets. `validate_profile` takes the parsed
YAML and returns a normalised copy, or raises ConfigurationError listing every problem at once with
its field path, so a person fixing a profile sees the whole list in one go. Pure: no I/O.
"""
from __future__ import annotations

import copy
import re

from domain.client_config import OPTIONAL_COMPONENTS
from domain.errors import ConfigurationError

REPO_KINDS = ("databricks", "adf", "fabric")
ENVIRONMENTS = ("prod", "dev", "uat")
HOST_OSES = ("windows", "linux")
_REPO_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_PROJECT = re.compile(r"^[A-Z][A-Z0-9_]*$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


def _required(errors: list[str], data: dict, path: str, key: str) -> object:
    value = data.get(key) if isinstance(data, dict) else None
    if value in (None, "", [], {}):
        errors.append(f"{path}.{key} is required")
    return value


def validate_profile(raw: dict | None) -> dict:
    """The normalised profile. Defaults filled in: display_name (engine name), host_os (windows),
    components (all on), model, schemas. Sections for a disabled component may be absent."""
    if not isinstance(raw, dict):
        raise ConfigurationError("client/profile.yaml is empty or not a mapping.")
    p = copy.deepcopy(raw)
    errors: list[str] = []

    client = p.setdefault("client", {}) or {}
    p["client"] = client
    _required(errors, client, "client", "name")
    client.setdefault("display_name", "Scotri")
    client["display_name"] = client.get("display_name") or "Scotri"
    client["logo"] = client.get("logo") or ""
    client["host_os"] = (client.get("host_os") or "windows").lower()
    if client["host_os"] not in HOST_OSES:
        errors.append(f"client.host_os must be one of {list(HOST_OSES)}")

    components = p.get("components") or {}
    unknown = sorted(set(components) - set(OPTIONAL_COMPONENTS))
    if unknown:
        errors.append(f"components: unknown {unknown}; optional components are {list(OPTIONAL_COMPONENTS)} "
                      f"(Databricks and Jira are always on)")
    p["components"] = {c: bool(components.get(c, True)) for c in OPTIONAL_COMPONENTS}

    jira = p.get("jira") or {}
    p["jira"] = jira
    site = _required(errors, jira, "jira", "site_url")
    if isinstance(site, str) and not site.startswith("https://"):
        errors.append("jira.site_url must start with https://")
    projects = [str(x).strip().upper() for x in (jira.get("projects") or []) if str(x).strip()]
    if not projects:
        errors.append("jira.projects needs at least one project key")
    for key in projects:
        if not _PROJECT.match(key):
            errors.append(f"jira.projects: '{key}' is not a Jira project key (letters, digits, _)")
    jira["projects"] = projects

    dbx = p.get("databricks") or {}
    p["databricks"] = dbx
    _required(errors, dbx, "databricks", "host")
    dbx["host"] = str(dbx.get("host") or "").removeprefix("https://").rstrip("/")
    dbx["warehouse_id"] = str(dbx.get("warehouse_id") or "")
    catalogs = dbx.get("catalogs") or {}
    if not catalogs.get("prod"):
        errors.append("databricks.catalogs.prod is required")
    for env, name in catalogs.items():
        if env not in ENVIRONMENTS:
            errors.append(f"databricks.catalogs: unknown environment '{env}' (use {list(ENVIRONMENTS)})")
        elif name and not _IDENTIFIER.match(str(name)):
            errors.append(f"databricks.catalogs.{env}: '{name}' is not a catalog name")
    dbx["catalogs"] = {k: str(v) for k, v in catalogs.items() if v}
    dbx["schemas"] = [str(s) for s in (dbx.get("schemas") or ["bronze", "silver", "gold"])]
    secondary = dbx.get("secondary_workspace") or None
    if secondary is not None:
        _required(errors, secondary, "databricks.secondary_workspace", "host")
        secondary["purpose"] = secondary.get("purpose") or ""
    dbx["secondary_workspace"] = secondary

    p["model"] = p.get("model") or "claude-sonnet-5-5"

    repos = p.get("repos") or []
    if not repos:
        errors.append("repos needs at least the Databricks repo")
    seen: set[str] = set()
    kinds: set[str] = set()
    for i, repo in enumerate(repos):
        path = f"repos[{i}]"
        if not isinstance(repo, dict):
            errors.append(f"{path} must be a mapping")
            continue
        key = _required(errors, repo, path, "repo_key")
        if isinstance(key, str):
            path = f"repos[{key}]"
            if not _REPO_KEY.match(key):
                errors.append(f"{path}.repo_key must be lower_snake_case (it is a --repo-key value)")
            if key in seen:
                errors.append(f"{path}: repo_key is used twice")
            seen.add(key)
        kind = repo.get("kind")
        if kind not in REPO_KINDS:
            errors.append(f"{path}.kind must be one of {list(REPO_KINDS)}")
        else:
            kinds.add(kind)
            if kind in OPTIONAL_COMPONENTS and not p["components"][kind]:
                errors.append(f"{path}: kind '{kind}' but components.{kind} is false — remove the repo "
                              f"or enable the component")
        _required(errors, repo, path, "ado_org")
        _required(errors, repo, path, "ado_project")
        repo["note"] = repo.get("note") or ""
        repo["legacy"] = bool(repo.get("legacy", False))  # an older structure kept live beside a newer one
        envs = repo.get("environments") or []
        if not envs:
            errors.append(f"{path}.environments needs at least one entry")
        for j, env in enumerate(envs):
            epath = f"{path}.environments[{j}]"
            if env.get("environment") not in ENVIRONMENTS:
                errors.append(f"{epath}.environment must be one of {list(ENVIRONMENTS)}")
            _required(errors, env, epath, "branch")
            env["mirrored"] = bool(env.get("mirrored", env.get("environment") == "prod"))
            if not (env.get("ado_repo") or repo.get("ado_repo")):
                errors.append(f"{epath}: ado_repo is needed here or on the repo")
            if kind == "databricks" and not env.get("catalog"):
                env["catalog"] = dbx["catalogs"].get(env.get("environment"), "")
    if "databricks" not in kinds:
        errors.append("repos: one repo of kind 'databricks' is required")
    for component in OPTIONAL_COMPONENTS:
        if p["components"][component] and component not in kinds:
            errors.append(f"components.{component} is on but no repo has kind '{component}'")

    if p["components"]["fabric"]:
        fabric = p.get("fabric") or {}
        p["fabric"] = fabric
        fabric["tenant_id"] = str(fabric.get("tenant_id") or "")
        workspaces = fabric.get("workspaces") or []
        for j, ws in enumerate(workspaces):
            _required(errors, ws, f"fabric.workspaces[{j}]", "name")
            if ws.get("repo_key") and ws["repo_key"] not in seen:
                errors.append(f"fabric.workspaces[{j}].repo_key '{ws['repo_key']}' is not in repos")
        fabric["workspaces"] = workspaces
    else:
        p["fabric"] = None

    if p["components"]["adf"]:
        adf = p.get("adf") or {}
        p["adf"] = adf
        adf["tenant_id"] = str(adf.get("tenant_id") or "")
        adf["client_id"] = str(adf.get("client_id") or "")
        factories = adf.get("factories") or []
        if not factories:
            errors.append("adf.factories needs at least one factory when components.adf is on")
        for j, f in enumerate(factories):
            _required(errors, f, f"adf.factories[{j}]", "factory_name")
            f["environment"] = f.get("environment") or "prod"
            f["subscription_id"] = str(f.get("subscription_id") or "")
            f["resource_group"] = str(f.get("resource_group") or "")
            f["default"] = bool(f.get("default", False))
        adf["factories"] = factories
    else:
        p["adf"] = None

    if errors:
        raise ConfigurationError("client/profile.yaml has problems:\n- " + "\n- ".join(errors))
    return p


def interpreter_path(profile: dict) -> str:
    """The venv interpreter the agent must call, relative to services/ (agent-instructions.md)."""
    return ".venv/Scripts/python.exe" if profile["client"]["host_os"] == "windows" else ".venv/bin/python"
