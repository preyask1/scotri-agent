"""Set up this workspace for one client: `python -m app.setup <step>`, run from services/ (SETUP.md).

    init       write client/profile.yaml (questions, or --from an existing profile)
    render     generate services/config/*.yaml and docs/ai/client/ from the profile
    secrets    collect credentials into the gitignored services/.env (input is never echoed)
    doctor     check every enabled component, read-only, and say how to fix what fails
    bootstrap  first mirror sync + lineage index, then doctor

A human runs these; the agent never does (agent-instructions.md). Every external call `doctor`
makes goes through the query governor with the cache off, so it is rate-counted and audited like any
other call and never answered from a stale cache.
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import getpass
import io
import json
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from domain.client_profile import validate_profile
from domain.client_setup import (credential_prompts, env_from_profile, render_generated, render_skeletons,
                                 update_env_text)
from domain.errors import ScotriError

SERVICES_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = SERVICES_ROOT.parent
PROFILE = Path("client") / "profile.yaml"
EXAMPLE_PROFILE = Path("examples") / "acme-retail" / "client" / "profile.yaml"
DOCTOR_USER = "setup:doctor"

PROFILE_HEADER = """# Client profile — the one file that describes this deployment (SETUP.md).
# Edit it, then run `python -m app.setup render` from services/. Identifiers and choices only:
# never put a secret here (`python -m app.setup secrets` writes those to services/.env).
# Annotated example: examples/acme-retail/client/profile.yaml
"""


def _out(message: str = "") -> None:
    print(message, flush=True)


def load_profile(root: Path) -> dict:
    path = root / PROFILE
    if not path.is_file():
        raise SystemExit(f"{path} does not exist. Run `python -m app.setup init` first (SETUP.md).")
    with open(path, "r", encoding="utf-8") as f:
        return validate_profile(yaml.safe_load(f))


# --- init ------------------------------------------------------------------------------------

def _ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer or default


def _ask_bool(question: str, default: bool = True) -> bool:
    answer = _ask(f"{question} (y/n)", "y" if default else "n").lower()
    return answer.startswith("y")


def interview() -> dict:
    """The questions `init` asks. Repos beyond one per kind, workspaces and factories can be added
    to the written profile by hand afterwards."""
    _out("Answer a few questions; press Enter to accept the [default]. Nothing here is secret.\n")
    name = _ask("Business name (as it should appear in docs)")
    display = _ask("Name users should see for the agent", "Scotri")
    host_os = _ask("Host OS the agent runs on (windows/linux)",
                   "windows" if platform.system() == "Windows" else "linux")
    site = _ask("Jira site URL, e.g. https://yourcompany.atlassian.net")
    projects = [p.strip().upper() for p in _ask("Jira project keys, comma-separated (primary first)").split(",") if p.strip()]
    adf_on = _ask_bool("Does the platform use Azure Data Factory?")
    fabric_on = _ask_bool("Does the platform use Power BI / Fabric?")
    host = _ask("Databricks workspace host, e.g. adb-1234567890123456.7.azuredatabricks.net")
    warehouse = _ask("Databricks SQL warehouse id for the agent", "")
    prod_cat = _ask("Prod Unity Catalog catalog name")
    dev_cat = _ask("Dev catalog name (blank if none)", "")
    org = _ask("Azure DevOps organisation holding the repos")
    project = _ask("Azure DevOps project")
    repos: list[dict] = []
    dbx_repo = _ask("Databricks (ETL notebooks) repo name")
    dbx_envs = [{"environment": "prod", "branch": _ask("  its Prod branch", "main"), "mirrored": True}]
    dev_branch = _ask("  its Dev branch (blank if none)", "")
    if dev_branch:
        dbx_envs.append({"environment": "dev", "branch": dev_branch, "mirrored": False})
    repos.append({"repo_key": "databricks", "kind": "databricks", "ado_org": org, "ado_project": project,
                  "ado_repo": dbx_repo, "environments": dbx_envs})
    profile: dict = {
        "client": {"name": name, "display_name": display, "logo": "", "host_os": host_os},
        "components": {"adf": adf_on, "fabric": fabric_on},
        "jira": {"site_url": site, "projects": projects},
        "databricks": {"host": host, "warehouse_id": warehouse,
                       "catalogs": {k: v for k, v in (("prod", prod_cat), ("dev", dev_cat)) if v},
                       "schemas": ["bronze", "silver", "gold"]},
        "model": "claude-sonnet-5-5",
        "repos": repos,
    }
    if adf_on:
        adf_repo = _ask("ADF Prod repo name")
        adf_envs = [{"environment": "prod", "branch": _ask("  its branch", "main"), "mirrored": True,
                     "ado_repo": adf_repo}]
        adf_dev = _ask("ADF Dev repo name (blank if none, or the same repo with a dev branch)", "")
        if adf_dev:
            adf_envs.append({"environment": "dev", "branch": _ask("  its branch", "main"), "mirrored": False,
                             "ado_repo": adf_dev})
        repos.append({"repo_key": "adf", "kind": "adf", "ado_org": org, "ado_project": project,
                      "environments": adf_envs})
        profile["adf"] = {"tenant_id": _ask("Entra tenant id the factories live in", ""),
                          "client_id": _ask("ADF reader app registration client id", ""),
                          "factories": [{"environment": "prod", "factory_name": _ask("Prod data factory name"),
                                         "subscription_id": _ask("  its subscription id", ""),
                                         "resource_group": _ask("  its resource group", ""), "default": True}]}
    if fabric_on:
        names = [n.strip() for n in _ask("Power BI repo names, comma-separated").split(",") if n.strip()]
        workspaces = []
        for i, repo_name in enumerate(names):
            key = "fabric" if len(names) == 1 else f"fabric_{i + 1}"
            branch = _ask(f"  Prod branch of {repo_name}", "main")
            repos.append({"repo_key": key, "kind": "fabric", "ado_org": org, "ado_project": project,
                          "ado_repo": repo_name, "environments": [{"environment": "prod", "branch": branch,
                                                                   "mirrored": True}]})
            ws = _ask(f"  Fabric workspace git-synced to {repo_name}/{branch} (blank if none)", "")
            if ws:
                workspaces.append({"name": ws, "repo_key": key, "branch": branch})
        profile["fabric"] = {"tenant_id": _ask("Entra tenant id Power BI/Fabric lives in", ""),
                             "workspaces": workspaces}
    return profile


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root)
    target = root / PROFILE
    if target.exists() and not args.force:
        _out(f"{target} already exists. Edit it directly, or pass --force to start over.")
        return 1
    if args.source:
        source = Path(args.source)
        with open(source, "r", encoding="utf-8") as f:
            validate_profile(yaml.safe_load(f))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    else:
        profile = interview()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(PROFILE_HEADER + "\n" + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True),
                          encoding="utf-8")
        try:
            validate_profile(profile)
        except ScotriError as e:
            _out(f"\nWrote {target}, but it needs fixing before render:\n{e.user_message}")
            return 1
    _out(f"\nWrote {target}. Review it (add more repos, workspaces or factories), then run "
         f"`python -m app.setup render`.")
    return 0


# --- render ----------------------------------------------------------------------------------

def plan_render(root: Path, profile: dict, force: bool = False) -> list[tuple[str, str, str]]:
    """(relative path, action, content) for every file render would touch. Actions: create, update,
    unchanged, keep (a person-written doc that already exists)."""
    plan = []
    for rel, content in render_generated(profile).items():
        path = root / rel
        if not path.exists():
            action = "create"
        else:
            action = "unchanged" if path.read_text(encoding="utf-8") == content else "update"
        plan.append((rel, action, content))
    for rel, content in render_skeletons(profile).items():
        path = root / rel
        if not path.exists():
            plan.append((rel, "create", content))
        elif force and path.read_text(encoding="utf-8") != content:
            plan.append((rel, "update", content))
        else:
            plan.append((rel, "keep", content))
    return plan


def cmd_render(args: argparse.Namespace) -> int:
    root = Path(args.root)
    profile = load_profile(root)
    plan = plan_render(root, profile, force=args.force)
    for rel, action, content in plan:
        _out(f"  {action:<9} {rel}")
        if args.dry_run and action == "update" and args.diff:
            old = (root / rel).read_text(encoding="utf-8").splitlines(keepends=True)
            sys.stdout.writelines(difflib.unified_diff(old, content.splitlines(keepends=True), rel, rel))
        if not args.dry_run and action in ("create", "update"):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    kept = [rel for rel, action, _ in plan if action == "keep"]
    skeletons = set(render_skeletons(profile))
    created = [rel for rel, action, _ in plan if action == "create" and rel in skeletons]
    _out("\nDry run: nothing written." if args.dry_run else "\nDone.")
    if created:
        _out("New domain docs contain TODO sections for a person to fill in (SETUP.md, step 5).")
    if kept:
        _out("Person-written docs were left as they are (--force regenerates them as skeletons).")
    return 0


# --- secrets ---------------------------------------------------------------------------------

def cmd_secrets(args: argparse.Namespace) -> int:
    root = Path(args.root)
    profile = load_profile(root)
    env_path = root / "services" / ".env"
    example = root / "services" / ".env.example"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else example.read_text(encoding="utf-8")
    values = dict(env_from_profile(profile))
    if not _parse_env(text).get("JIRA_CLOUD_ID"):
        cloud_id = fetch_jira_cloud_id(profile["jira"]["site_url"])
        if cloud_id:
            values["JIRA_CLOUD_ID"] = cloud_id
            _out(f"Fetched JIRA_CLOUD_ID from {profile['jira']['site_url']}.")
    if not args.non_interactive:
        current = _parse_env(text)
        _out("Paste each credential; input is hidden for secrets. Enter keeps the current value "
             "(shown as 'set'). Values are written only to services/.env, which git ignores.\n")
        for name, label, secret in credential_prompts(profile):
            state = "set" if current.get(name) else "empty"
            prompt = f"{name} — {label} [{state}]: "
            value = getpass.getpass(prompt) if secret else input(prompt)
            if value.strip():
                values[name] = value.strip()
    env_path.write_text(update_env_text(text, values), encoding="utf-8")
    _out(f"\nUpdated {env_path} ({len(values)} value(s) set; none printed). For a vault-based deployment, "
         f"put the same variable names in the vault instead (SETUP.md).")
    return 0


def fetch_jira_cloud_id(site_url: str) -> str | None:
    """The site's cloud id from its public, unauthenticated tenant_info endpoint (no credential is
    sent), or None if it can't be reached; then `secrets` asks for it instead."""
    import requests

    try:
        resp = requests.get(site_url.rstrip("/") + "/_edge/tenant_info", timeout=10)
        resp.raise_for_status()
        return str(resp.json().get("cloudId") or "") or None
    except Exception:  # noqa: BLE001 — offline or blocked: fall back to asking
        return None


def _parse_env(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.split("#", 1)[0].strip().strip('"')
    return values


# --- doctor ----------------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    status: str  # ok | fail | skip
    detail: str = ""
    fix: str = ""


def _governed(container, backend, label: str, execute):
    """One external check through the governor, cache off: rate-counted and audited, never stale."""
    from domain.governed_call import run_governed
    from domain.governor_policy import GovernorPolicy

    result, _ = run_governed(user=DOCTOR_USER, backend=backend, cache_key_input=f"doctor:{label}",
                             rate_limiter=container.rate_limiter, cache=container.cache, policy=GovernorPolicy(),
                             execute=execute, serialize=json.dumps, deserialize=json.loads, use_cache=False)
    return result


CHECK_TIMEOUT_SECONDS = 45
"""Per network check. SDKs retry an unreachable host for minutes; doctor reports that instead."""


def _attempt(name: str, fn, fix: str, timeout: float | None = CHECK_TIMEOUT_SECONDS) -> Check:
    """Runs one check in a daemon thread so a hung connection can't stall doctor or its exit."""
    import threading

    box: dict = {}

    def run() -> None:
        try:
            box["check"] = _attempt_now(name, fn, fix)
        except BaseException as e:  # noqa: BLE001
            box["check"] = Check(name, "fail", f"{type(e).__name__}: {e}"[:400], fix)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return Check(name, "fail", f"no answer within {timeout:.0f}s (the SDK may still be retrying)",
                     fix + "; also check the host name and that this machine can reach it")
    return box["check"]


def _attempt_now(name: str, fn, fix: str) -> Check:
    try:
        detail = fn()
        return Check(name, "ok", detail or "")
    except ScotriError as e:
        if "environment variable" in e.user_message:
            fix = "run `python -m app.setup secrets` (a required setting is missing)"
        return Check(name, "fail", e.user_message, fix)
    except Exception as e:  # noqa: BLE001 — doctor reports every failure, it never crashes on one
        return Check(name, "fail", f"{type(e).__name__}: {e}"[:400], fix)


def run_doctor(root: Path, network: bool = True) -> list[Check]:
    from app.container import Container
    from domain.models import Backend

    checks: list[Check] = []
    try:
        profile = load_profile(root)
        checks.append(Check("profile", "ok", f"{profile['client']['name']} ({root / PROFILE})"))
    except (SystemExit, ScotriError) as e:
        message = e.user_message if isinstance(e, ScotriError) else str(e)
        return [Check("profile", "fail", message, "fix client/profile.yaml, or run `python -m app.setup init`")]

    stale = [rel for rel, action, _ in plan_render(root, profile) if action in ("create", "update")]
    checks.append(Check("generated files", "fail" if stale else "ok",
                        ("out of date: " + ", ".join(stale)) if stale else "match the profile",
                        "run `python -m app.setup render`" if stale else ""))

    container = Container()
    secrets = container.secrets
    optional = {"ADF_MANAGED_IDENTITY_OBJECT_ID", "AZURE_AD_TENANT_ID", "AZURE_AD_API_AUDIENCE"}
    missing = [name for name, _, _ in credential_prompts(profile)
               if name not in optional and not secrets.get_optional(name)]
    checks.append(Check("credentials", "fail" if missing else "ok",
                        ("not set: " + ", ".join(missing)) if missing else "every required setting is set",
                        "run `python -m app.setup secrets`" if missing else ""))
    dev_auth = (secrets.get_optional("SCOTRI_AUTH_MODE", "") or "").lower() == "dev"
    entra = secrets.get_optional("AZURE_AD_TENANT_ID") and secrets.get_optional("AZURE_AD_API_AUDIENCE")
    if entra:
        checks.append(Check("API auth", "ok", "Entra ID"))
    elif dev_auth:
        checks.append(Check("API auth", "ok", "dev mode (SCOTRI_AUTH_MODE=dev): local use only, never production"))
    else:
        checks.append(Check("API auth", "fail", "neither Entra ID nor dev auth is configured",
                            "set AZURE_AD_TENANT_ID + AZURE_AD_API_AUDIENCE, or SCOTRI_AUTH_MODE=dev locally"))

    redis = _attempt("redis", lambda: "reachable" if container._redis_client.ping() else "no reply",
                     "start Redis or fix REDIS_HOST/REDIS_PORT/REDIS_PASSWORD")
    checks.append(redis)
    if profile["adf"]:
        checks.append(_attempt("ADF factories", lambda: _factories(container),
                               "fill subscription_id/resource_group in client/profile.yaml and re-render"))
    if not network:
        return checks + [Check("backends", "skip", "--offline: Azure DevOps, Databricks, Jira, Fabric not checked")]
    if redis.status != "ok":
        return checks + [Check("backends", "skip", "every backend check goes through the query governor, "
                                                   "which needs Redis: fix Redis first")]

    try:
        refs = [(key, ref) for key in container.registry.all_repo_keys() for ref in container.registry.get(key)]
    except ScotriError as e:
        refs = []
        checks.append(Check("repo registry", "fail", e.user_message, "run `python -m app.setup render`"))
    for key, ref in refs:
        if ref.mirrored:
            checks.append(_attempt(f"ADO {key}@{ref.branch}", lambda ref=ref: _ado(container, ref),
                                   f"check the PAT for org '{ref.org}' (Code (Read)) and that repo "
                                   f"'{ref.repo}' has branch '{ref.branch}'"))
    prod = profile["databricks"]["catalogs"]["prod"]
    checks.append(_attempt(
        "Databricks", lambda: _schemas(_governed(container, Backend.DATABRICKS, f"dbx:{prod}",
                                                 lambda: _rows(container.databricks.execute(prod, f"SHOW SCHEMAS IN {prod}")))),
        f"grant the service principal USE CATALOG/USE SCHEMA/SELECT on {prod} and CAN USE on the warehouse"))
    email = secrets.get_optional("JIRA_EMAIL", "")
    checks.append(_attempt(
        "Jira", lambda: "service account found" if _governed(container, Backend.JIRA, f"jira:{email}",
                                                             lambda: container.jira_writer.find_account_id(email)) else "no match",
        "check JIRA_EMAIL/JIRA_API_TOKEN scopes (read:jira-work, read:jira-user, write:jira-work) and JIRA_CLOUD_ID"))
    if profile["fabric"]:
        for ws in profile["fabric"]["workspaces"]:
            checks.append(_attempt(
                f"Fabric {ws['name']}",
                lambda ws=ws: "workspace visible" if _governed(container, Backend.FABRIC, f"fabric:{ws['name']}",
                                                               lambda: container.fabric._resolve_workspace_id(ws["name"])) else "",
                "add the Fabric service principal to the workspace (Viewer) and allow service principals in the "
                "Fabric tenant settings"))
    else:
        checks.append(Check("Fabric", "skip", "not enabled for this deployment"))
    if profile["adf"]:
        on_azure = bool(secrets.get_optional("ADF_MANAGED_IDENTITY_OBJECT_ID") or
                        secrets.get_optional("ADF_FEDERATED_TOKEN_FILE"))
        checks.append(Check("ADF run history", "skip",
                            "token needs the host's managed identity: check from the Azure host" if not on_azure
                            else "managed identity configured; run `pipeline-runs` once to confirm the Reader grant"))
    else:
        checks.append(Check("ADF", "skip", "not enabled for this deployment"))
    mirror = _attempt("mirror", lambda: _mirror(container), "run `python -m app.setup bootstrap` for the first sync")
    checks.append(mirror)
    return checks


def _ado(container, ref) -> str:
    from domain.models import Backend

    entries = _governed(container, Backend.ADO, f"ado:{ref.org}:{ref.repo}:{ref.branch}",
                        lambda: container.live_repo_reader(ref.org, ref.project).list_dir(ref.repo, ref.branch, "/"))
    return f"{len(entries)} entries at the root"


def _rows(result) -> list:
    rows = getattr(result, "rows", result)
    return [list(r) if isinstance(r, (list, tuple)) else r for r in rows]


def _schemas(rows: list) -> str:
    return f"{len(rows)} schema(s) visible"


def _factories(container) -> str:
    from domain.models import Environment

    ref = container.adf_factories.get(Environment.PROD)
    return f"default factory {ref.factory_name} has its ids"


def _mirror(container) -> str:
    status = container.mirror_reader.read_status()
    if not status:
        raise RuntimeError("no mirror sync has run yet")
    return f"last sync {status.get('last_run_at', 'unknown')}"


def print_checks(checks: list[Check]) -> int:
    width = max(len(c.name) for c in checks)
    for c in checks:
        mark = {"ok": "PASS", "fail": "FAIL", "skip": "SKIP"}[c.status]
        _out(f"  {mark}  {c.name:<{width}}  {c.detail}")
        if c.fix:
            _out(f"        {'':<{width}}  fix: {c.fix}")
    failed = sum(c.status == "fail" for c in checks)
    _out(f"\n{failed} check(s) failed." if failed else "\nAll checks passed.")
    return 1 if failed else 0


def cmd_doctor(args: argparse.Namespace) -> int:
    return print_checks(run_doctor(Path(args.root), network=not args.offline))


# --- bootstrap -------------------------------------------------------------------------------

def cmd_bootstrap(args: argparse.Namespace) -> int:
    from app import cli

    root = Path(args.root)
    load_profile(root)
    for argv, what in ((["sync-run-once"], "first mirror sync (can take several minutes)"),
                       (["lineage-rebuild"], "lineage index")):
        _out(f"Running {what}…")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(argv)
        _out(buffer.getvalue().strip()[-2000:])
        if code != 0:
            _out(f"\n`{' '.join(argv)}` failed; fix that first (`python -m app.setup doctor` helps).")
            return code
    _out("\nChecking everything:")
    return print_checks(run_doctor(root))


# --- entry point -----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.setup", description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=str(WORKSPACE_ROOT), help="workspace root (default: this checkout)")
    sub = parser.add_subparsers(dest="step", required=True)
    p = sub.add_parser("init", help="write client/profile.yaml")
    p.add_argument("--from", dest="source", help=f"copy and validate an existing profile, e.g. {EXAMPLE_PROFILE}")
    p.add_argument("--force", action="store_true", help="overwrite an existing profile")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("render", help="generate config and client docs from the profile")
    p.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    p.add_argument("--diff", action="store_true", help="with --dry-run, print the diff of each update")
    p.add_argument("--force", action="store_true", help="also reset person-written docs to skeletons")
    p.set_defaults(func=cmd_render)
    p = sub.add_parser("secrets", help="collect credentials into services/.env")
    p.add_argument("--non-interactive", action="store_true", help="only write the values the profile knows")
    p.set_defaults(func=cmd_secrets)
    p = sub.add_parser("doctor", help="check every enabled component (read-only)")
    p.add_argument("--offline", action="store_true", help="skip checks that call Azure DevOps/Databricks/Jira/Fabric")
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser("bootstrap", help="first mirror sync + lineage index, then doctor")
    p.set_defaults(func=cmd_bootstrap)
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ScotriError as e:
        _out(e.user_message)
        return 1


if __name__ == "__main__":
    sys.exit(main())
