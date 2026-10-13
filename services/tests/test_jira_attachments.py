# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Jira attachments (OPEN_ITEMS.md #42): jira-get-attachment saves readable, redacted copies under
scratch/jira-attachments/<KEY>/; jira-attach only uploads Scotri's own output or the caller's uploads,
redacted, behind the same --confirmed gate as every other Jira write."""
from __future__ import annotations

import argparse
import io
import json
import zipfile
from pathlib import Path

import pytest

import app.cli as cli
from domain.errors import NotFoundError, PolicyError
from domain.jira_attachments import find_attachments, office_to_text, prepare_upload, safe_filename, save_attachment

SECRET = "password=hunter2hunter2"


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
DOCX = _zip({"word/document.xml": f"""<w:document {W}><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Scope</w:t></w:r></w:p>
<w:p><w:r><w:t>Add a column to </w:t></w:r><w:r><w:t>gold.sales</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>KPI</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>{SECRET}</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body></w:document>"""})

S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
XLSX = _zip({
    "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>',
    "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                  '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
    "xl/sharedStrings.xml": f"<sst {S}><si><t>Region</t></si><si><t>North</t></si></sst>",
    "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData>'
                                '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1"><v>42</v></c></row>'
                                '<row r="2"><c r="A2" t="s"><v>1</v></c></row></sheetData></worksheet>',
})

A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
PPTX = _zip({"ppt/slides/slide2.xml": f"<s {A}><a:p><a:r><a:t>Second</a:t></a:r></a:p></s>",
             "ppt/slides/slide1.xml": f"<s {A}><a:p><a:r><a:t>First</a:t></a:r></a:p></s>"})


def test_docx_extracts_headings_paragraphs_and_tables():
    text = office_to_text("brd.docx", DOCX)
    assert "# Scope" in text
    assert "Add a column to gold.sales" in text
    assert "| KPI |" in text


def test_xlsx_extracts_rows_with_column_positions():
    text = office_to_text("s.xlsx", XLSX)
    assert "## Sheet: Data" in text
    assert "Region\t\t42" in text
    assert "North" in text


def test_a_sheet_far_larger_than_the_budget_still_yields_its_first_rows(monkeypatch):
    """A raw-data export (ACME-2118's 'Division A - visit report' xlsx) was refused outright because the
    whole sheet was charged to the budget up front. Streaming stops at the row cap and charges only
    what it read, and only the shared strings those rows use are resolved."""
    import domain.jira_attachments as ja
    monkeypatch.setattr(ja, "_MAX_SHEET_ROWS", 3)
    monkeypatch.setattr(ja, "_MAX_OFFICE_XML_BYTES", 200_000)
    rows = "".join(f'<row r="{i}"><c r="A{i}" t="s"><v>{i % 2}</v></c><c r="B{i}"><v>{i}</v></c></row>'
                   for i in range(1, 40_001))  # ~2.6 MB of sheet XML, 13x the budget
    big_strings = "".join(f"<si><t>unused {i}</t></si>" for i in range(200_000))
    xlsx = _zip({
        "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="Raw" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                      '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/sharedStrings.xml": f"<sst {S}><si><t>Even</t></si><si><t>Odd</t></si>{big_strings}</sst>",
        "xl/worksheets/sheet1.xml": f'<worksheet {S}><dimension ref="A1:B40000"/><sheetData>{rows}</sheetData></worksheet>',
    })
    text = office_to_text("export.xlsx", xlsx)
    assert "## Sheet: Raw (range A1:B40000)" in text
    assert text.splitlines()[1:4] == ["Odd\t1", "Even\t2", "Odd\t3"]
    assert "truncated after 3 rows; the sheet's range is A1:B40000" in text


def test_a_zip_bomb_is_still_refused(monkeypatch):
    import domain.jira_attachments as ja
    monkeypatch.setattr(ja, "_MAX_OFFICE_XML_BYTES", 50_000)
    rows = "".join(f'<row r="{i}"><c r="A{i}"/></row>' for i in range(1, 20_001))  # empty rows: never hits the cap
    xlsx = _zip({
        "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="X" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                      '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData>{rows}</sheetData></worksheet>',
    })
    with pytest.raises(PolicyError, match="extraction limit"):
        office_to_text("bomb.xlsx", xlsx)


def test_pptx_orders_slides_numerically():
    text = office_to_text("d.pptx", PPTX)
    assert text.index("First") < text.index("Second")


def test_bad_office_file_is_a_policy_error():
    with pytest.raises(PolicyError):
        office_to_text("x.docx", b"not a zip")


def test_safe_filename_cannot_escape():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("..\\..\\a b$.csv") == "a b_.csv"
    assert safe_filename("...") == "attachment"


def test_find_attachments_only_on_this_ticket():
    issue = {"key": "ACME-1", "fields": {"attachment": [{"id": "10", "filename": "a.pdf"}]}}
    assert find_attachments(issue, None) == [{"id": "10", "filename": "a.pdf"}]
    assert find_attachments(issue, "10")[0]["filename"] == "a.pdf"
    with pytest.raises(NotFoundError):
        find_attachments(issue, "99")


def test_save_office_as_redacted_text(tmp_path):
    saved = save_attachment(tmp_path, "ACME-1", "10", "BRD v2.docx", DOCX)
    assert saved.kind == "extracted"
    assert saved.path == "scratch/jira-attachments/ACME-1/10-BRD v2.docx.txt"
    body = (tmp_path / saved.path).read_text()
    assert "hunter2" not in body and saved.redacted
    assert not (tmp_path / "scratch/jira-attachments/ACME-1/10-BRD v2.docx").exists()


def test_save_text_redacted_and_binary_as_is(tmp_path):
    t = save_attachment(tmp_path, "ACME-1", "11", "notes.csv", f"a,{SECRET}\n".encode())
    assert "hunter2" not in (tmp_path / t.path).read_text()
    b = save_attachment(tmp_path, "ACME-1", "12", "shot.png", b"\x89PNG data")
    assert b.kind == "binary" and (tmp_path / b.path).read_bytes() == b"\x89PNG data"


def test_save_refuses_unreadable_format_and_bad_key(tmp_path):
    with pytest.raises(PolicyError):
        save_attachment(tmp_path, "ACME-1", "13", "bundle.zip", b"PK")
    with pytest.raises(PolicyError):
        save_attachment(tmp_path, "../ACME-1", "13", "a.txt", b"x")


# --- upload policy ---------------------------------------------------------------------------------


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "scratch").mkdir()
    (tmp_path / "proposals" / "ACME-1").mkdir(parents=True)
    (tmp_path / "services" / "mirror").mkdir(parents=True)
    return tmp_path


def test_upload_allows_own_deliverables_and_redacts(ws):
    (ws / "scratch" / "ACME-1-handoff.md").write_text(f"# Handoff\n{SECRET}\n")
    up = prepare_upload(ws, "scratch/ACME-1-handoff.md")
    assert up.filename == "ACME-1-handoff.md" and up.redacted
    assert b"hunter2" not in up.data
    (ws / "proposals" / "ACME-1" / "fix.patch").write_text("--- a\n+++ b\n")
    assert prepare_upload(ws, str(ws / "proposals/ACME-1/fix.patch"), filename="ACME-1 fix.patch").filename == "ACME-1 fix.patch"


@pytest.mark.parametrize("rel", ["services/mirror/x.md", "CLAUDE.md"])
def test_upload_refuses_outside_output_roots(ws, rel):
    (ws / rel).write_text("x")
    with pytest.raises(PolicyError):
        prepare_upload(ws, rel)


def test_upload_refuses_a_symlink_out_of_the_workspace(ws, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.md"
    outside.write_text("x")
    try:
        (ws / "scratch" / "link.md").symlink_to(outside)
    except OSError as e:  # Windows without Developer Mode/admin can't create symlinks (WinError 1314)
        pytest.skip(f"cannot create symlinks here: {e}")
    with pytest.raises(PolicyError):
        prepare_upload(ws, "scratch/link.md")


def test_upload_refuses_escape_and_downloads(ws):
    (ws.parent / "sibling.md").write_text("x")
    with pytest.raises(PolicyError):
        prepare_upload(ws, "scratch/../../sibling.md")
    d = ws / "scratch" / "jira-attachments" / "ACME-1"
    d.mkdir(parents=True)
    (d / "10-a.txt").write_text("x")
    with pytest.raises(PolicyError):
        prepare_upload(ws, "scratch/jira-attachments/ACME-1/10-a.txt")


def test_upload_refuses_type_size_and_missing(ws, monkeypatch):
    (ws / "scratch" / "a.pem").write_bytes(b"x")
    with pytest.raises(PolicyError):
        prepare_upload(ws, "scratch/a.pem")
    (ws / "scratch" / "a.docx").write_bytes(b"x")
    assert prepare_upload(ws, "scratch/a.docx").filename == "a.docx"
    with pytest.raises(NotFoundError):
        prepare_upload(ws, "scratch/nope.md")
    import domain.jira_attachments as ja
    monkeypatch.setattr(ja, "MAX_UPLOAD_BYTES", 3)
    (ws / "scratch" / "big.md").write_text("12345")
    with pytest.raises(PolicyError):
        prepare_upload(ws, "scratch/big.md")


# --- CLI ------------------------------------------------------------------------------------------


class _Limiter:
    def enter_concurrency_slot(self, backend): return 1
    def exit_concurrency_slot(self, backend): pass
    def increment_and_get_count(self, user, window_seconds): return 1


class _Cache:
    def __init__(self): self.store = {}
    def get(self, k): return self.store.get(k)
    def set(self, k, v, ttl_seconds): self.store[k] = v


class _Reader:
    def __init__(self):
        self.downloads = []

    def get_issue(self, key):
        return {"key": key, "fields": {"attachment": [
            {"id": "10", "filename": "brd.docx", "size": len(DOCX)},
            {"id": "11", "filename": "dump.zip", "size": 5},
        ]}}

    def download_attachment(self, attachment_id, max_bytes):
        self.downloads.append(attachment_id)
        return DOCX


def test_get_attachment_saves_readable_and_reports_skipped(monkeypatch, tmp_path, capsys):
    reader = _Reader()
    cache = _Cache()
    container = type("C", (), {"jira": reader, "rate_limiter": _Limiter(), "cache": cache})()
    monkeypatch.setattr(cli, "build_container", lambda: container)
    monkeypatch.setattr(cli, "WORKSPACE_ROOT", tmp_path)
    args = argparse.Namespace(key="ACME-7", attachment_id=None, user="u")
    code = cli.cmd_jira_get_attachment(args)
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["attachment_count"] == 2
    assert [s["filename"] for s in out["saved"]] == ["brd.docx"]
    assert out["skipped"][0]["filename"] == "dump.zip"
    assert reader.downloads == ["10"]  # the unreadable one was never downloaded
    assert (tmp_path / out["saved"][0]["path"]).exists()
    assert cache.store == {}  # neither the issue nor the bytes are cached

    code = cli.cmd_jira_get_attachment(argparse.Namespace(key="ACME-7", attachment_id="10", user="u"))
    assert code == 0 and reader.downloads == ["10", "10"]


def test_attach_refuses_without_confirmed(monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_container", lambda: pytest.fail("must not build"))
    code = cli.cmd_jira_attach(argparse.Namespace(confirmed=False, key="ACME-1", file="scratch/x.md",
                                                  filename=None, user="u"))
    assert code == 1 and json.loads(capsys.readouterr().out)["category"] == "policy"


def test_attach_refuses_bad_source_before_building_container(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "build_container", lambda: pytest.fail("must not build"))
    monkeypatch.setattr(cli, "WORKSPACE_ROOT", tmp_path)
    (tmp_path / "CLAUDE.md").write_text("x")
    code = cli.cmd_jira_attach(argparse.Namespace(confirmed=True, key="ACME-1", file="CLAUDE.md",
                                                  filename=None, user="u"))
    assert code == 1 and json.loads(capsys.readouterr().out)["category"] == "policy"


def test_attach_uploads_redacted_content(monkeypatch, tmp_path, capsys):
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "ACME-1-handoff.md").write_text(f"x {SECRET}")
    sent = {}

    class _Writer:
        def add_attachment(self, key, filename, data, content_type):
            sent.update(key=key, filename=filename, data=data, content_type=content_type)
            return [{"id": "500", "filename": filename, "size": len(data)}]

    monkeypatch.setattr(cli, "build_container", lambda: type("C", (), {"jira_writer": _Writer()})())
    monkeypatch.setattr(cli, "WORKSPACE_ROOT", tmp_path)
    code = cli.cmd_jira_attach(argparse.Namespace(confirmed=True, key="ACME-1", file="scratch/ACME-1-handoff.md",
                                                  filename=None, user="u"))
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["attachments"][0]["id"] == "500" and out["redacted"] is True
    assert sent["key"] == "ACME-1" and sent["content_type"] == "text/markdown"
    assert b"hunter2" not in sent["data"]


def test_attach_is_not_batchable_but_download_is():
    assert "jira-attach" not in cli.BATCHABLE_COMMANDS
    assert "jira-get-attachment" in cli.BATCHABLE_COMMANDS
