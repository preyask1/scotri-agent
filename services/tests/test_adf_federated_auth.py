# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""ADF run-history auth is a federated identity credential — no secret anywhere (OPEN_ITEMS.md #16).
adapters/azure_ad/federated_assertion.py finds the managed-identity token; the ADF reader sends it as
a client_assertion. HTTP is faked; these assert the wiring and the failure messages, not Entra."""
from __future__ import annotations

import pytest
import requests

from adapters.adf import rest_run_reader_adapter as adapter_mod
from adapters.azure_ad import federated_assertion as fa
from domain.errors import CredentialError


class _Secrets:
    def __init__(self, **values):
        self._v = values

    def get(self, name):
        return self._v[name]

    def get_optional(self, name, default=None):
        return self._v.get(name) or default


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = {"content-type": "application/json"}
        self.text = ""

    def json(self):
        return self._payload


def test_token_file_is_read_and_stripped(tmp_path):
    f = tmp_path / "token"
    f.write_text("  assertion-1\n", encoding="utf-8")
    assert fa.get_client_assertion(_Secrets(ADF_FEDERATED_TOKEN_FILE=str(f))) == "assertion-1"


def test_empty_token_file_is_a_credential_error(tmp_path):
    f = tmp_path / "token"
    f.write_text("", encoding="utf-8")
    with pytest.raises(CredentialError):
        fa.get_client_assertion(_Secrets(ADF_FEDERATED_TOKEN_FILE=str(f)))


def test_imds_used_with_object_id_and_exchange_audience(monkeypatch):
    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    monkeypatch.delenv("IDENTITY_HEADER", raising=False)
    seen = {}

    def fake_get(url, params, headers, timeout):
        seen.update(url=url, params=params, headers=headers)
        return _Resp(payload={"access_token": "mi-token"})

    monkeypatch.setattr(fa.requests, "get", fake_get)
    out = fa.get_client_assertion(_Secrets(ADF_MANAGED_IDENTITY_OBJECT_ID="oid-1"))
    assert out == "mi-token"
    assert seen["url"].startswith("http://169.254.169.254/")
    assert seen["params"]["resource"] == "api://AzureADTokenExchange"
    assert seen["params"]["object_id"] == "oid-1"
    assert seen["headers"] == {"Metadata": "true"}


def test_app_service_endpoint_used_when_present(monkeypatch):
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:42356/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "hdr")
    seen = {}

    def fake_get(url, params, headers, timeout):
        seen.update(url=url, headers=headers)
        return _Resp(payload={"access_token": "t"})

    monkeypatch.setattr(fa.requests, "get", fake_get)
    assert fa.get_client_assertion(_Secrets()) == "t"
    assert seen["url"] == "http://localhost:42356/msi/token"
    assert seen["headers"] == {"X-IDENTITY-HEADER": "hdr"}


def test_laptop_without_a_managed_identity_gets_a_clear_error(monkeypatch):
    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    monkeypatch.delenv("IDENTITY_HEADER", raising=False)

    def boom(*a, **k):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(fa.requests, "get", boom)
    with pytest.raises(CredentialError, match="Azure host"):
        fa.get_client_assertion(_Secrets())


def test_reader_exchanges_assertion_without_any_secret(monkeypatch):
    monkeypatch.setattr(adapter_mod, "get_client_assertion", lambda secrets, prefix: "the-assertion")
    posted = {}

    def fake_post(url, data, timeout):
        posted.update(url=url, data=data)
        return _Resp(payload={"access_token": "arm-token", "expires_in": 3600})

    monkeypatch.setattr(adapter_mod.requests, "post", fake_post)
    reader = adapter_mod.AdfRestRunReader(_Secrets(ADF_TENANT_ID="t-1", ADF_CLIENT_ID="app-1"))
    assert reader._get_token() == "arm-token"
    assert "t-1" in posted["url"]
    assert posted["data"]["client_id"] == "app-1"
    assert posted["data"]["client_assertion"] == "the-assertion"
    assert posted["data"]["client_assertion_type"].endswith("jwt-bearer")
    assert "client_secret" not in posted["data"]
