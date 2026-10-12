"""White-labelling (SETUP.md): the client profile, `python -m app.setup render`, component gating,
catalog-aware grounding and page branding."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

import pytest
import yaml

import app.cli as cli
from app import setup as setup_cli
from domain.branding import brand_html
from domain.client_config import (DEFAULT_CLIENT_CONFIG, ClientConfig, check_command_enabled,
                                  client_config_from_dict)
from domain.client_profile import validate_profile
from domain.client_setup import (ado_pat_names, credential_prompts, env_from_profile, render_generated,
                                 render_skeletons, update_env_text)
from domain.errors import ConfigurationError
from domain.grounding import check_grounding, extract_claims

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "acme-retail"


def _example() -> dict:
    return yaml.safe_load((EXAMPLE / "client" / "profile.yaml").read_text(encoding="utf-8"))


def _without(profile: dict, component: str) -> dict:
    p = copy.deepcopy(profile)
    p["components"][component] = False
    p["repos"] = [r for r in p["repos"] if r["kind"] != component]
    p.pop(component, None)
    if component == "fabric":
        p.pop("fabric", None)
    return p


# --- profile validation ------------------------------------------------------------------------

def test_the_example_profile_is_valid_and_gets_defaults():
    p = validate_profile(_example())
    assert p["components"] == {"adf": True, "fabric": True}
    dbx = next(r for r in p["repos"] if r["kind"] == "databricks")
    assert [e["catalog"] for e in dbx["environments"]] == ["acme_prod", "acme_dev"]


def test_every_problem_is_listed_at_once():
    raw = _example()
    raw["jira"]["site_url"] = "http://not-https"
    raw["databricks"]["catalogs"] = {}
    raw["repos"][0]["repo_key"] = "Bad-Key"
    with pytest.raises(ConfigurationError) as err:
        validate_profile(raw)
    message = err.value.user_message
    assert "jira.site_url must start with https://" in message
    assert "databricks.catalogs.prod is required" in message
    assert "lower_snake_case" in message


def test_a_disabled_component_needs_no_section_but_refuses_its_repos():
    validate_profile(_without(_example(), "adf"))
    raw = _example()
    raw["components"]["adf"] = False
    with pytest.raises(ConfigurationError, match="kind 'adf' but components.adf is false"):
        validate_profile(raw)


def test_an_enabled_component_needs_a_repo_of_its_kind():
    raw = _example()
    raw["repos"] = [r for r in raw["repos"] if r["kind"] != "fabric"]
    with pytest.raises(ConfigurationError, match="components.fabric is on but no repo"):
        validate_profile(raw)


# --- render ------------------------------------------------------------------------------------

def test_rendering_the_example_reproduces_its_generated_files():
    """The golden check: examples/acme-retail is exactly what `render` writes for its profile."""
    for rel, content in render_generated(validate_profile(_example())).items():
        assert (EXAMPLE / rel).read_text(encoding="utf-8") == content, rel


def test_render_writes_skeletons_once_and_never_overwrites_them(tmp_path):
    (tmp_path / "client").mkdir()
    (tmp_path / "client" / "profile.yaml").write_text((EXAMPLE / "client" / "profile.yaml").read_text())
    args = argparse.Namespace(root=str(tmp_path), dry_run=False, diff=False, force=False)
    assert setup_cli.cmd_render(args) == 0
    lineage_map = tmp_path / "docs" / "ai" / "client" / "lineage-map.md"
    assert "Mapped chains" in lineage_map.read_text()
    lineage_map.write_text("written by a person")
    assert setup_cli.cmd_render(args) == 0
    assert lineage_map.read_text() == "written by a person"
    actions = {a for _, a, _ in setup_cli.plan_render(tmp_path, validate_profile(_example()))}
    assert actions <= {"unchanged", "keep"}


def test_switching_a_component_off_removes_it_from_config_and_docs():
    files = render_generated(validate_profile(_without(_example(), "adf")))
    registry = yaml.safe_load(files["services/config/repo_registry.yaml"])
    assert "adf" not in [r["repo_key"] for r in registry["repos"]]
    assert yaml.safe_load(files["services/config/adf_factories.yaml"]) == {"factories": []}
    assert yaml.safe_load(files["services/config/client.yaml"])["components"] == {"adf": False, "fabric": True}
    assert "| Azure Data Factory | **off** |" in files["docs/ai/client/client.md"]
    assert "## ADF factories" not in files["docs/ai/client/client.md"]


def test_skeletons_follow_the_components():
    names = set(render_skeletons(validate_profile(_without(_example(), "fabric"))))
    assert "docs/ai/client/fabric-architecture.md" not in names
    assert "docs/ai/client/databricks-architecture.md" in names


def test_the_profile_name_and_display_name_reach_the_docs():
    raw = _example()
    raw["client"].update(name="Contoso", display_name="Contoso Data Desk", host_os="linux")
    files = render_generated(validate_profile(raw))
    client_md = files["docs/ai/client/client.md"]
    assert "# Client facts: Contoso" in client_md
    assert "call yourself **Contoso Data Desk**" in client_md
    assert "services/.venv/bin/python" in client_md
    assert yaml.safe_load(files["services/config/client.yaml"])["display_name"] == "Contoso Data Desk"


# --- secrets -----------------------------------------------------------------------------------

def test_one_pat_per_ado_org_and_secrets_only_for_enabled_components():
    p = validate_profile(_example())
    assert ado_pat_names(p) == ["ADO_PAT_READONLY", "ADO_PAT_READONLY_ACMEFABRICREPORTS"]
    names = [n for n, _, _ in credential_prompts(validate_profile(_without(_example(), "fabric")))]
    assert "FABRIC_SP_CLIENT_SECRET" not in names and "DATABRICKS_CLIENT_SECRET" in names
    assert env_from_profile(p)["DATABRICKS_HOST"] == "https://adb-1111111111111111.11.azuredatabricks.net"


def test_update_env_text_replaces_in_place_and_appends_the_rest():
    text = "# comment\nJIRA_EMAIL=\"\"   # keep me out\nREDIS_HOST=localhost\n"
    out = update_env_text(text, {"JIRA_EMAIL": "bot@example.com", "NEW_VAR": "a b"})
    assert "JIRA_EMAIL=bot@example.com\n" in out
    assert "REDIS_HOST=localhost" in out and "# comment" in out
    assert 'NEW_VAR="a b"' in out


# --- component gating --------------------------------------------------------------------------

def test_client_config_defaults_and_refuses_unknown_components():
    assert client_config_from_dict(None) == DEFAULT_CLIENT_CONFIG
    assert client_config_from_dict({"components": {"adf": False}}).components == frozenset({"fabric"})
    with pytest.raises(ConfigurationError, match="unknown component"):
        client_config_from_dict({"components": {"powerbi": False}})


def test_a_disabled_component_refuses_its_commands_only():
    config = ClientConfig(components=frozenset({"fabric"}))
    with pytest.raises(ConfigurationError, match="not enabled for this deployment"):
        check_command_enabled("pipeline-runs", config)
    check_command_enabled("dax-execute", config)
    check_command_enabled("databricks-select", config)


def test_the_cli_refuses_before_building_any_adapter(monkeypatch, capsys):
    class _Container:
        client_config = ClientConfig(components=frozenset())

        def __getattr__(self, name):  # any adapter access would be a bug
            raise AssertionError(f"adapter {name} was built for a disabled component")

    monkeypatch.setattr(cli, "build_container", lambda mirror_root=None: _Container())
    code = cli.main(["refresh-history", "--user", "u", "--workspace", "w", "--dataset", "d"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 1 and payload["category"] == "configuration"
    assert "not enabled for this deployment" in payload["message"]


# --- grounding with configured catalogs --------------------------------------------------------

def test_grounding_checks_the_configured_catalogs():
    answer = "contoso_main.sales.orders grew; acme_prod.gold.x is unrelated."
    assert extract_claims(answer, ("contoso_main",)) == ["contoso_main.sales.orders"]
    assert extract_claims(answer) == ["acme_prod.gold.x"]  # generic *_prod / *_dev fallback
    assert not check_grounding(answer, "", ("contoso_main",)).ok


# --- branding ----------------------------------------------------------------------------------

def test_branding_renames_text_but_never_identifiers():
    page = ('<title>Scotri Admin</title><link rel="icon" href="x">'
            '<span id="brand-mark" class="a"><svg viewBox="0 0 48 48"></svg></span><span>Scotri</span>'
            '<code>scotri_start</code> <span class="scotri-anim"></span> Scotri\'s')
    out = brand_html(page, "Contoso <Desk>", "/brand/logo")
    assert "<title>Contoso &lt;Desk&gt; Admin</title>" in out
    assert "scotri_start" in out and "scotri-anim" in out and "Contoso &lt;Desk&gt;'s" in out
    assert '<link rel="icon" href="/brand/logo">' in out and '<img src="/brand/logo"' in out
    assert "<svg" not in out
    assert brand_html(page, "Scotri") == page


@pytest.mark.parametrize("value", ["yes", "1.5", "2026-10-01", "12:30", "a b", "x: y", "#c", "acme_prod",
                                   "https://x.y/z", "007", "", "it's", "line\nbreak", "1e5", "- a",
                                   "00000000-0000-0000-0000-000000000000", True, None, 3])
def test_generated_yaml_reads_back_as_the_same_value(value):
    """domain/client_setup.py writes YAML with the stdlib (the domain layer has no third-party imports);
    every scalar must read back unchanged, never as a bool, number, date or comment."""
    from domain.client_setup import _block_yaml

    data = {"k": value, "nested": [{"a": [value, {"b": value}]}, []], "empty": {}}
    assert yaml.safe_load(_block_yaml(data)) == data
