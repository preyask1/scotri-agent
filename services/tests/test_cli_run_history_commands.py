# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""app/cli.py's pipeline-runs / activity-runs / refresh-history — the actual invocation points for
adf-databricks-read's "ADF pipeline failures" section and fabric-read's "Fabric refresh failures"
recipe (OPEN_ITEMS.md: decided 2026-09-27). Fakes stand in for the governed adapters/rate-limiter/
cache, same style as test_cli_governor_check.py — these assert the wiring (guardrails enforced
before the adapter is called; the right adapter method called with the right arguments; the result
shape on stdout), not the real HTTP calls themselves.
"""
from __future__ import annotations

import argparse
import json

import app.cli as cli
from domain.models import AdfFactoryRef, Backend, Environment


class _FakeRateLimiter:
    def increment_and_get_count(self, user, window_seconds):
        return 1

    def enter_concurrency_slot(self, backend):
        return 1

    def exit_concurrency_slot(self, backend):
        pass


class _FakeCache:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl_seconds):
        self.store[key] = value


class _FakeAdfFactories:
    def get(self, environment, factory_name=None):
        return AdfFactoryRef(
            environment=environment, subscription_id="sub-1", resource_group="rg", factory_name=factory_name or "f"
        )


class _FakeAdfRuns:
    def __init__(self):
        self.pipeline_calls = []
        self.activity_calls = []
        self.trigger_calls = []
        # Configurable so a test can force the empty-result (hint) path.
        self.pipeline_result = None   # None => a default single Failed run
        self.trigger_result = None    # None => a default single Succeeded trigger run

    def query_pipeline_runs(self, factory, pipeline_names, start, end):
        self.pipeline_calls.append((factory, pipeline_names, start, end))
        if self.pipeline_result is not None:
            return self.pipeline_result
        return [{"runId": "run-1", "pipelineName": pipeline_names[0], "status": "Failed"}]

    def query_activity_runs(self, factory, run_id, start, end):
        self.activity_calls.append((factory, run_id, start, end))
        return [{"activityName": "Copy1", "status": "Failed", "error": {"message": "PathNotFound"}}]

    def query_trigger_runs(self, factory, trigger_names, start, end):
        self.trigger_calls.append((factory, trigger_names, start, end))
        if self.trigger_result is not None:
            return self.trigger_result
        return [{"triggerName": trigger_names[0], "status": "Succeeded"}]


class _FakeFabric:
    def __init__(self):
        self.history_calls = []
        self.detail_calls = []

    def get_refresh_history(self, workspace, model, top):
        self.history_calls.append((workspace, model, top))
        return [{"refreshId": "r1", "status": "Failed"}]

    def get_refresh_execution_details(self, workspace, model, refresh_id):
        self.detail_calls.append((workspace, model, refresh_id))
        return {"available": True, "refreshId": refresh_id}


class _FakeContainer:
    def __init__(self):
        self.rate_limiter = _FakeRateLimiter()
        self.cache = _FakeCache()
        self.adf_factories = _FakeAdfFactories()
        self.adf_runs = _FakeAdfRuns()
        self.fabric = _FakeFabric()


def _install_fake_container(monkeypatch):
    container = _FakeContainer()
    monkeypatch.setattr(cli, "build_container", lambda: container)
    return container


def test_pipeline_runs_calls_adapter_with_resolved_factory_and_window(monkeypatch, capsys):
    container = _install_fake_container(monkeypatch)
    args = argparse.Namespace(
        user="alice", environment="prod", pipeline=["PL_Forecast_Sales"],
        start="2026-09-26T00:00:00Z", end="2026-09-27T00:00:00Z",
    )
    exit_code = cli.cmd_pipeline_runs(args)
    payload = json.loads(capsys.readouterr().out.strip())

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["run_count"] == 1
    assert payload["runs"][0]["pipelineName"] == "PL_Forecast_Sales"
    (factory, pipeline_names, start, end) = container.adf_runs.pipeline_calls[0]
    assert factory.factory_name == "f"
    assert pipeline_names == ["PL_Forecast_Sales"]
    assert start.isoformat().startswith("2026-09-26")


def test_pipeline_runs_refuses_empty_pipeline_list(monkeypatch, capsys):
    """Defense in depth: argparse's nargs='+' already stops an empty --pipeline at the shell
    boundary, but a direct/programmatic call still must not reach the adapter unfiltered."""
    container = _install_fake_container(monkeypatch)
    args = argparse.Namespace(
        user="alice", environment="prod", pipeline=[],
        start="2026-09-26T00:00:00Z", end="2026-09-27T00:00:00Z",
    )
    exit_code = cli.cmd_pipeline_runs(args)
    payload = json.loads(capsys.readouterr().out.strip())

    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["category"] == "policy"
    assert container.adf_runs.pipeline_calls == []  # never reached the adapter


def test_pipeline_runs_refuses_window_over_cap(monkeypatch, capsys):
    container = _install_fake_container(monkeypatch)
    args = argparse.Namespace(
        user="alice", environment="prod", pipeline=["PL_Forecast_Sales"],
        start="2026-01-01T00:00:00Z", end="2026-09-27T00:00:00Z",  # far over the 14-day cap
    )
    exit_code = cli.cmd_pipeline_runs(args)
    payload = json.loads(capsys.readouterr().out.strip())

    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["category"] == "policy"
    assert container.adf_runs.pipeline_calls == []


def test_pipeline_runs_hint_when_zero_runs(monkeypatch, capsys):
    """Zero pipeline runs must not read as 'the pipeline failed' — the tool surfaces the
    disabled/failed-trigger distinction itself (OPEN_ITEMS.md #35)."""
    container = _install_fake_container(monkeypatch)
    container.adf_runs.pipeline_result = []
    args = argparse.Namespace(
        user="alice", environment="prod", pipeline=["PL_Forecast_Sales"],
        start="2026-09-26T00:00:00Z", end="2026-09-27T00:00:00Z",
    )
    exit_code = cli.cmd_pipeline_runs(args)
    payload = json.loads(capsys.readouterr().out.strip())
    assert exit_code == 0 and payload["run_count"] == 0
    assert "trigger-runs" in payload["hint"]


def test_trigger_runs_calls_adapter_with_resolved_factory_and_names(monkeypatch, capsys):
    container = _install_fake_container(monkeypatch)
    args = argparse.Namespace(
        user="alice", environment="prod", trigger=["Trg_Daily_Forecast"],
        start="2026-09-26T00:00:00Z", end="2026-09-27T00:00:00Z",
    )
    exit_code = cli.cmd_trigger_runs(args)
    payload = json.loads(capsys.readouterr().out.strip())

    assert exit_code == 0 and payload["ok"] is True
    assert payload["run_count"] == 1
    assert payload["trigger_runs"][0]["triggerName"] == "Trg_Daily_Forecast"
    (factory, trigger_names, start, end) = container.adf_runs.trigger_calls[0]
    assert factory.factory_name == "f" and trigger_names == ["Trg_Daily_Forecast"]
    assert start.isoformat().startswith("2026-09-26")


def test_trigger_runs_refuses_empty_trigger_list(monkeypatch, capsys):
    container = _install_fake_container(monkeypatch)
    args = argparse.Namespace(
        user="alice", environment="prod", trigger=[],
        start="2026-09-26T00:00:00Z", end="2026-09-27T00:00:00Z",
    )
    exit_code = cli.cmd_trigger_runs(args)
    payload = json.loads(capsys.readouterr().out.strip())
    assert exit_code == 1 and payload["category"] == "policy"
    assert container.adf_runs.trigger_calls == []  # never reached the adapter


def test_trigger_runs_refuses_window_over_cap(monkeypatch, capsys):
    container = _install_fake_container(monkeypatch)
    args = argparse.Namespace(
        user="alice", environment="prod", trigger=["Trg_Daily_Forecast"],
        start="2026-01-01T00:00:00Z", end="2026-09-27T00:00:00Z",  # far over the 14-day cap
    )
    exit_code = cli.cmd_trigger_runs(args)
    payload = json.loads(capsys.readouterr().out.strip())
    assert exit_code == 1 and payload["category"] == "policy"
    assert container.adf_runs.trigger_calls == []


def test_trigger_runs_hint_when_zero_runs(monkeypatch, capsys):
    container = _install_fake_container(monkeypatch)
    container.adf_runs.trigger_result = []
    args = argparse.Namespace(
        user="alice", environment="prod", trigger=["Trg_Daily_Forecast"],
        start="2026-09-26T00:00:00Z", end="2026-09-27T00:00:00Z",
    )
    exit_code = cli.cmd_trigger_runs(args)
    payload = json.loads(capsys.readouterr().out.strip())
    assert exit_code == 0 and payload["run_count"] == 0
    assert "enabled" in payload["hint"]


def test_activity_runs_calls_adapter_with_run_id(monkeypatch, capsys):
    container = _install_fake_container(monkeypatch)
    args = argparse.Namespace(
        user="alice", environment="prod", run_id="run-1",
        start="2026-09-26T00:00:00Z", end="2026-09-27T00:00:00Z",
    )
    exit_code = cli.cmd_activity_runs(args)
    payload = json.loads(capsys.readouterr().out.strip())

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["activity_runs"][0]["status"] == "Failed"
    (factory, run_id, start, end) = container.adf_runs.activity_calls[0]
    assert run_id == "run-1"


def test_refresh_history_lists_when_no_refresh_id_given(monkeypatch, capsys):
    container = _install_fake_container(monkeypatch)
    args = argparse.Namespace(user="alice", workspace="WS", dataset="DS", top=10, refresh_id=None)
    exit_code = cli.cmd_refresh_history(args)
    payload = json.loads(capsys.readouterr().out.strip())

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["attempt_count"] == 1
    assert container.fabric.history_calls == [("WS", "DS", 10)]
    assert container.fabric.detail_calls == []


def test_refresh_history_drills_into_execution_detail_when_refresh_id_given(monkeypatch, capsys):
    container = _install_fake_container(monkeypatch)
    args = argparse.Namespace(user="alice", workspace="WS", dataset="DS", top=10, refresh_id="r1")
    exit_code = cli.cmd_refresh_history(args)
    payload = json.loads(capsys.readouterr().out.strip())

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["execution_detail"]["refreshId"] == "r1"
    assert container.fabric.detail_calls == [("WS", "DS", "r1")]
    assert container.fabric.history_calls == []


def test_pipeline_runs_passes_named_factory_to_registry_and_adapter(monkeypatch, capsys):
    container = _install_fake_container(monkeypatch)
    args = argparse.Namespace(
        user="alice", environment="prod", factory="acme-analytics-adf", pipeline=["P1"],
        start="2026-09-26T00:00:00Z", end="2026-09-27T00:00:00Z",
    )
    assert cli.cmd_pipeline_runs(args) == 0
    capsys.readouterr()
    assert container.adf_runs.pipeline_calls[0][0].factory_name == "acme-analytics-adf"
