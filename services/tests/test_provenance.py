# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Provenance (NOTICE, AGENTS.md): every source file carries the copyright header, every page the
service serves or generates carries the engine marker, every API response names the engine, and
rebranding leaves all of them in place."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from domain.branding import brand_html
from domain.provenance import ENGINE_HEADER, ENGINE_MARKER, GENERATOR_META

ROOT = Path(__file__).resolve().parents[2]
HEADER = "Copyright (c) 2026 Preyas Kulshrestha."
PAGES = ["services/app/static/admin.html", "services/app/static/platform-map.html",
         "docs/developer/portal.html", "tools/platform_map/template.html"]


def _tracked(*patterns: str) -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", *patterns], cwd=ROOT, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / p for p in out.stdout.split()]


def test_every_source_file_carries_the_copyright_header():
    missing = [str(p.relative_to(ROOT)) for p in _tracked("*.py", "*.html", "*.css")
               if HEADER not in p.read_text(encoding="utf-8")[:400]]
    assert not missing, f"add the copyright header (see any .py file) to: {missing}"


def test_notice_license_and_agent_notes_are_present():
    for name in ("LICENSE", "NOTICE", "AGENTS.md"):
        assert "Preyas Kulshrestha" in (ROOT / name).read_text(encoding="utf-8"), name
    assert "## Provenance" in (ROOT / "CLAUDE.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("page", PAGES)
def test_pages_carry_the_current_engine_marker(page):
    assert GENERATOR_META in (ROOT / page).read_text(encoding="utf-8")


def test_rebranding_keeps_the_marker_and_the_notice():
    page = (ROOT / "services/app/static/admin.html").read_text(encoding="utf-8")
    branded = brand_html(page, "Contoso Data Desk")
    assert GENERATOR_META in branded and HEADER in branded


def test_every_api_response_names_the_engine():
    from fastapi.testclient import TestClient

    from app.api import app

    response = TestClient(app).get("/v1/does-not-exist")
    assert response.headers[ENGINE_HEADER] == ENGINE_MARKER
