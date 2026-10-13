# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""/platform-map serves a data-free sign-in shell; /v1/platform-map serves the generated page, only
to an authenticated caller."""
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.api as api
from domain.models import Principal


class _Container:
    def __init__(self, path: Path):
        self._path = path

    def path_setting(self, name, default):
        assert name == "PLATFORM_MAP_PATH"
        return self._path


@pytest.fixture()
def client(tmp_path, monkeypatch):
    page = tmp_path / "platform-map.html"
    monkeypatch.setattr(api, "container", _Container(page))

    def authenticate(authorization):
        if authorization != "Bearer good":
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        return Principal(subject="alice", display_name="Alice", platform="scotri-ui", roles=[])

    monkeypatch.setattr(api, "_authenticate", authenticate)
    return TestClient(api.app), page


def test_shell_is_served_without_a_token_and_carries_no_map(client):
    c, page = client
    page.write_text("<title>Data Platform Map</title><script id=\"data\">{\"secret\": 1}</script>", encoding="utf-8")
    r = c.get("/platform-map")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "/v1/platform-map" in r.text and "secret" not in r.text
    assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"


def test_map_needs_a_token(client):
    c, page = client
    page.write_text("<p>map</p>", encoding="utf-8")
    assert c.get("/v1/platform-map").status_code == 401
    assert c.get("/v1/platform-map", headers={"Authorization": "Bearer bad"}).status_code == 401


def test_map_is_served_to_an_authenticated_caller(client):
    c, page = client
    page.write_text("<p>map</p>", encoding="utf-8")
    r = c.get("/v1/platform-map", headers={"Authorization": "Bearer good"})
    assert r.status_code == 200 and r.text == "<p>map</p>"
    assert r.headers["content-type"].startswith("text/html") and r.headers["x-platform-map-built"]


def test_map_not_built_yet_is_a_404_that_says_how_to_build_it(client):
    c, _ = client
    r = c.get("/v1/platform-map", headers={"Authorization": "Bearer good"})
    assert r.status_code == 404 and "gen_platform_map.py" in r.json()["detail"]
