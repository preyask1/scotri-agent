# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Path settings resolve under services/, whatever directory a process starts in (OPEN_ITEMS.md #62)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.container import SERVICES_ROOT, Container

SETTINGS = {
    "MIRROR_ROOT": lambda c: c.mirror_root,
    "LINEAGE_SOURCE_PATH": lambda c: c.lineage_source_path,
    "INTERACTION_LOG_PATH": lambda c: c.interaction_log._path,
    "ENGAGEMENT_HISTORY_DB_PATH": lambda c: c.path_setting("ENGAGEMENT_HISTORY_DB_PATH", Path("unused")),
}


@pytest.mark.parametrize("name", SETTINGS)
def test_a_relative_path_setting_resolves_under_services_not_the_working_directory(monkeypatch, tmp_path, name):
    monkeypatch.chdir(tmp_path)  # e.g. an API started from the workspace root
    monkeypatch.setenv(name, "./some/where.x")
    assert SETTINGS[name](Container()) == SERVICES_ROOT / "some" / "where.x"


def test_an_absolute_path_setting_is_used_as_given(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_STORE_DIR", str(tmp_path / "files"))
    assert Container().path_setting("ARTIFACT_STORE_DIR", Path("unused")) == tmp_path / "files"


def test_an_unset_path_setting_falls_back_to_its_default(monkeypatch):
    monkeypatch.setenv("SOME_UNSET_PATH", "")
    assert Container().path_setting("SOME_UNSET_PATH", SERVICES_ROOT / "d") == SERVICES_ROOT / "d"
