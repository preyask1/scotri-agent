"""app/cli.py — the maintenance-flag short-circuit in front of every mirrored (Prod) read
(agent-instructions.md: "never read mirror-active/ while the maintenance flag is set"; fabric-read /
adf-databricks-read SKILL.md's Path A).

Uses a fake MaintenanceFlag/mirror reader (both satisfy their ports.* Protocol by duck typing) so
this runs without a real mirror on disk or a live maintenance-flag store."""
from __future__ import annotations

import argparse
import json

import app.cli as cli
from domain.models import Environment, MaintenanceStatus, RepoRef


class _FakeMaintenanceFlag:
    def __init__(self, active: bool, eta_message: str | None = None):
        self._active = active
        self._eta_message = eta_message

    def status(self, repo: str) -> MaintenanceStatus:
        return MaintenanceStatus(active=self._active, eta_message=self._eta_message)


class _FakeMirrorReader:
    def __init__(self, status=None, file_content="file content"):
        self.list_dir_called = False
        self.read_file_called = False
        self._status = status  # what read_status() returns (freshness, OPEN_ITEMS.md #31)
        self._file_content = file_content

    def list_dir(self, repo, branch, path):
        self.list_dir_called = True
        return [{"name": "pipeline", "type": "dir"}]

    def read_file(self, repo, branch, path):
        self.read_file_called = True
        return self._file_content

    def read_status(self):
        return self._status


class _FakeSecrets:
    def get_optional(self, key, default=None):
        return default


class _FakeRegistry:
    """Satisfies ports.repo_registry.RepoRegistry by duck typing — one repo_key, one prod ref,
    matching cli.py's mirror-list/mirror-read resolving --repo-key through domain.env_resolution.resolve."""

    def get(self, repo_key: str) -> list[RepoRef]:
        assert repo_key == "adf"
        return [RepoRef(repo="AcmeAdfProd", org="acme-data", project="Data-Platform",
                         branch="main", environment=Environment.PROD, mirrored=True)]


class _FakeContainer:
    def __init__(self, maintenance_active: bool, eta_message: str | None = None, status=None, file_content="file content"):
        self.maintenance_flag = _FakeMaintenanceFlag(maintenance_active, eta_message)
        self.mirror_reader = _FakeMirrorReader(status=status, file_content=file_content)
        self.registry = _FakeRegistry()
        self.secrets = _FakeSecrets()


def _args(**overrides):
    base = {"repo_key": "adf", "environment": None, "branch": None, "path": ""}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_mirror_list_refuses_during_maintenance_window(monkeypatch, capsys):
    container = _FakeContainer(maintenance_active=True, eta_message="~90s")
    monkeypatch.setattr(cli, "build_container", lambda: container)

    exit_code = cli.cmd_mirror_list(_args())
    payload = json.loads(capsys.readouterr().out.strip())

    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["category"] == "transient"
    assert "90s" in payload["message"]
    assert container.mirror_reader.list_dir_called is False, (
        "must never touch the mirror while mid-swap, not even to attempt the read"
    )


def test_mirror_read_refuses_during_maintenance_window(monkeypatch, capsys):
    container = _FakeContainer(maintenance_active=True, eta_message="~90s")
    monkeypatch.setattr(cli, "build_container", lambda: container)

    exit_code = cli.cmd_mirror_read(_args(path="pipeline/PL_Gold_Orchestrator.json"))
    payload = json.loads(capsys.readouterr().out.strip())

    assert exit_code == 1
    assert payload["ok"] is False
    assert container.mirror_reader.read_file_called is False


def test_mirror_list_proceeds_once_flag_is_clear(monkeypatch, capsys):
    container = _FakeContainer(maintenance_active=False)
    monkeypatch.setattr(cli, "build_container", lambda: container)

    exit_code = cli.cmd_mirror_list(_args())
    payload = json.loads(capsys.readouterr().out.strip())

    assert exit_code == 0
    assert payload["ok"] is True
    assert container.mirror_reader.list_dir_called is True
    # Freshness is always reported (OPEN_ITEMS.md #31); no status.json here -> "unknown", not fresh.
    assert payload["freshness"]["available"] is False
    assert "unknown" in payload["freshness"]["warning"]


def test_mirror_list_surfaces_stale_warning_without_blocking(monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
    status = {"last_run_at": old, "overall_ok": True,
              "repos": [{"repo": "AcmeAdfProd", "branch": "main", "ok": True, "checked_at": old}]}
    container = _FakeContainer(maintenance_active=False, status=status)
    monkeypatch.setattr(cli, "build_container", lambda: container)

    exit_code = cli.cmd_mirror_list(_args())
    payload = json.loads(capsys.readouterr().out.strip())

    assert exit_code == 0 and payload["ok"] is True          # advisory: read still succeeds
    assert payload["freshness"]["stale"] is True
    assert "stale" in payload["freshness"]["warning"]


def test_mirror_read_proceeds_once_flag_is_clear(monkeypatch, capsys):
    container = _FakeContainer(maintenance_active=False)
    monkeypatch.setattr(cli, "build_container", lambda: container)

    exit_code = cli.cmd_mirror_read(_args(path="pipeline/PL_Gold_Orchestrator.json"))
    payload = json.loads(capsys.readouterr().out.strip())

    assert exit_code == 0
    assert payload["ok"] is True
    assert container.mirror_reader.read_file_called is True


def test_mirror_read_redacts_secrets_in_content(monkeypatch, capsys):
    """A hardcoded credential in a mirror notebook must not reach the agent's context via stdout
    (OPEN_ITEMS.md #34) — the CLI output boundary redacts it."""
    secret_content = 'spark.conf.set("fs.azure.account.key", "AccountKey=SUPERSECRETVALUE123456==")'
    container = _FakeContainer(maintenance_active=False, file_content=secret_content)
    monkeypatch.setattr(cli, "build_container", lambda: container)

    exit_code = cli.cmd_mirror_read(_args(path="notebooks/load.py"))
    raw = capsys.readouterr().out.strip()

    assert exit_code == 0
    assert "SUPERSECRETVALUE123456" not in raw          # the value is gone
    assert "[REDACTED]" in raw                            # replaced, not just dropped
    assert json.loads(raw)["ok"] is True                 # still valid JSON
