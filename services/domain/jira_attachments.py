"""Jira attachments, both directions (OPEN_ITEMS.md #42, built 2026-10-05).

Read (`jira-get-attachment`): an attachment listed on a ticket is downloaded through the governor and
saved under scratch/jira-attachments/<KEY>/ in a form the agent can actually read with its `Read`
tool:
- text formats are decoded and secret-redacted before they touch disk;
- .docx/.xlsx/.pptx (BRDs, sample sheets, decks: the usual Jira attachments) are converted to a
  redacted `.txt` with the stdlib only, so no new dependency and no raw Office file on disk;
- PDFs and images are saved as sent (the `Read` tool renders them); they are not redacted, the same
  gap OPEN_ITEMS.md #6(d) records for uploads;
- anything else (zip, msg, exe, ...) is refused with its metadata, never written.

Write (`jira-attach`): the fourth jira-write action. Only a file in Scotri's own output or the caller's
own uploads can be attached (scratch/, proposals/, investigations/), never a mirror file, never
something outside the workspace, never a downloaded Jira attachment bounced back. Text files are
redacted before upload, so what lands on the ticket is no less redacted than a published deliverable.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath

from domain.errors import NotFoundError, PolicyError
from domain.redaction import redact_secrets
from domain.shared_files import DEFAULT_MAX_UPLOAD_BYTES, DEFAULT_UPLOAD_SUFFIXES, TEXT_SUFFIXES, content_type_for, is_blocked

DOWNLOAD_DIR = PurePosixPath("scratch", "jira-attachments")
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
"""Download cap. Jira's own default limit is 10 MB but sites raise it; 25 MB covers a fat BRD PDF."""
MAX_UPLOAD_BYTES = DEFAULT_MAX_UPLOAD_BYTES
OFFICE_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx"})
BINARY_READABLE_SUFFIXES = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"})
READABLE_SUFFIXES = TEXT_SUFFIXES | OFFICE_SUFFIXES | BINARY_READABLE_SUFFIXES

UPLOAD_ROOTS = ("scratch", "proposals", "investigations")
UPLOAD_SUFFIXES = DEFAULT_UPLOAD_SUFFIXES

_MAX_OFFICE_XML_BYTES = 50 * 1024 * 1024
"""Total decompressed XML read from one Office file: a guard against a zip bomb."""
_MAX_SHEET_ROWS = 5000
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()+-]")
_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


def require_issue_key(key: str) -> str:
    if not _KEY.match(key or ""):
        raise PolicyError(f"not a Jira issue key: {key!r}")
    return key


def safe_filename(name: str) -> str:
    """A filename that can't escape its directory or collide with a dotfile."""
    base = PurePosixPath((name or "").replace("\\", "/")).name
    cleaned = _UNSAFE.sub("_", base).strip(" .")
    return cleaned or "attachment"


def find_attachments(issue: dict, attachment_id: str | None) -> list[dict]:
    """The ticket's attachment metadata, or the one asked for. An id that isn't on THIS ticket is
    not found: the command reads a ticket's attachments, not any attachment the account can see."""
    attachments = (issue.get("fields") or {}).get("attachment") or []
    if attachment_id is None:
        return attachments
    match = [a for a in attachments if str(a.get("id")) == str(attachment_id)]
    if not match:
        raise NotFoundError(f"attachment {attachment_id} is not on {issue.get('key', 'this ticket')}")
    return match


# --- Office text extraction (stdlib only) ---------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


class _Zip:
    def __init__(self, data: bytes) -> None:
        try:
            self._zip = zipfile.ZipFile(BytesIO(data))
        except zipfile.BadZipFile as e:
            raise PolicyError(f"not a valid Office file: {e}") from e
        self._budget = _MAX_OFFICE_XML_BYTES

    def names(self) -> list[str]:
        return self._zip.namelist()

    def xml(self, name: str) -> ET.Element | None:
        try:
            info = self._zip.getinfo(name)
        except KeyError:
            return None
        self._spend(info.file_size)
        return ET.fromstring(self._zip.read(info))

    def stream(self, name: str):
        """A file object over one part that charges the budget for the bytes actually read, so a
        reader that stops early (a sheet past the row cap) pays only for what it read."""
        try:
            info = self._zip.getinfo(name)
        except KeyError:
            return None
        return _Budgeted(self._zip.open(info), self._spend)

    def _spend(self, n: int) -> None:
        self._budget -= n
        if self._budget < 0:
            raise PolicyError("Office file expands past the 50 MB extraction limit; ask for a PDF/CSV export")


class _Budgeted:
    def __init__(self, raw, spend) -> None:
        self._raw, self._spend = raw, spend

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        self._spend(len(data))
        return data

    def close(self) -> None:
        self._raw.close()


def _docx_text(z: _Zip) -> str:
    root = z.xml("word/document.xml")
    if root is None:
        raise PolicyError("not a Word document (word/document.xml missing)")
    out: list[str] = []
    body = root.find(f"{_W}body")
    for block in (body if body is not None else []):
        if block.tag == f"{_W}p":
            out.append(_docx_para(block))
        elif block.tag == f"{_W}tbl":
            for row in block.iter(f"{_W}tr"):
                cells = [" ".join(_docx_para(p) for p in tc.iter(f"{_W}p")).strip() for tc in row.iter(f"{_W}tc")]
                out.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
            out.append("")
    return "\n".join(out)


def _docx_para(p: ET.Element) -> str:
    parts = []
    for node in p.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    style = p.find(f"{_W}pPr/{_W}pStyle")
    text = "".join(parts)
    level = style.get(f"{_W}val", "") if style is not None else ""
    m = re.match(r"(?i)heading(\d)", level)
    if m and text.strip():
        return "#" * min(int(m.group(1)), 6) + " " + text
    if p.find(f"{_W}pPr/{_W}numPr") is not None and text.strip():
        return "- " + text
    return text


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]*", ref).group(0):
        n = n * 26 + (ord(ch) - 64)
    return max(n - 1, 0)


def _sheet_rows(stream) -> tuple[list[dict[int, tuple[str | None, str]]], str | None, bool]:
    """Up to _MAX_SHEET_ROWS non-empty rows of one sheet, read as a stream: each row maps a column to
    (cell type, raw value). Also the sheet's declared range (`<dimension ref>`), and whether rows
    were left unread. Shared-string indices stay unresolved here (see _shared_strings)."""
    rows: list[dict[int, tuple[str | None, str]]] = []
    dimension, truncated = None, False
    for event, el in ET.iterparse(stream, events=("start", "end")):
        if event == "start":
            if el.tag == f"{_S}dimension":
                dimension = el.get("ref")
            continue
        if el.tag != f"{_S}row":
            continue
        if len(rows) >= _MAX_SHEET_ROWS:
            truncated = True
            break
        cells: dict[int, tuple[str | None, str]] = {}
        for c in el.iter(f"{_S}c"):
            kind = c.get("t")
            if kind == "inlineStr":
                cells[_col_index(c.get("r", "A"))] = (None, "".join(t.text or "" for t in c.iter(f"{_S}t")))
                continue
            v = c.find(f"{_S}v")
            if v is not None and v.text is not None:
                cells[_col_index(c.get("r", "A"))] = (kind, v.text)
        el.clear()
        if cells:
            rows.append(cells)
    stream.close()
    return rows, dimension, truncated


def _shared_strings(z: _Zip, needed: set[int]) -> dict[int, str]:
    """Only the shared strings the read rows use, streamed, stopping after the highest one needed:
    in a large export this file can be bigger than the rows actually shown."""
    stream = z.stream("xl/sharedStrings.xml") if needed else None
    if stream is None:
        return {}
    last, found, index = max(needed), {}, -1
    for event, el in ET.iterparse(stream, events=("end",)):
        if el.tag != f"{_S}si":
            continue
        index += 1
        if index in needed:
            found[index] = "".join(t.text or "" for t in el.iter(f"{_S}t"))
        el.clear()
        if index >= last:
            break
    stream.close()
    return found


def _xlsx_text(z: _Zip) -> str:
    workbook = z.xml("xl/workbook.xml")
    if workbook is None:
        raise PolicyError("not an Excel workbook (xl/workbook.xml missing)")
    rels = z.xml("xl/_rels/workbook.xml.rels")
    targets = {}
    if rels is not None:
        for rel in rels:
            target = rel.get("Target", "")
            targets[rel.get("Id")] = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
    sheets = []  # (name, rows, dimension, truncated), or rows None when the sheet part is missing
    for sheet in workbook.iter(f"{_S}sheet"):
        path = targets.get(sheet.get(f"{_R}id"))
        stream = z.stream(path) if path else None
        sheets.append((sheet.get("name", "?"), *(_sheet_rows(stream) if stream else (None, None, False))))
    needed = {int(raw) for _, rows, _, _ in sheets for row in rows or [] for kind, raw in row.values()
              if kind == "s" and raw.isdigit()}
    shared = _shared_strings(z, needed)

    out: list[str] = []
    for name, rows, dimension, truncated in sheets:
        out.append(f"## Sheet: {name}" + (f" (range {dimension})" if dimension else ""))
        if rows is None:
            out += ["(sheet data not found)", ""]
            continue
        for cells in rows:
            values = {col: (shared.get(int(raw), "") if kind == "s" and raw.isdigit() else raw)
                      for col, (kind, raw) in cells.items()}
            out.append("\t".join(values.get(i, "").replace("\t", " ").replace("\n", " ")
                                 for i in range(max(values) + 1)))
        if truncated:
            out.append(f"... (truncated after {_MAX_SHEET_ROWS} rows; the sheet's range is {dimension or 'unknown'}. "
                       "Ask for a filtered export, or a CSV, for the rest)")
        out.append("")
    return "\n".join(out)


def _pptx_text(z: _Zip) -> str:
    slides = sorted(
        (n for n in z.names() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)),
    )
    if not slides:
        raise PolicyError("not a PowerPoint deck (no ppt/slides/slideN.xml)")
    out: list[str] = []
    for i, name in enumerate(slides, 1):
        root = z.xml(name)
        out.append(f"## Slide {i}")
        for p in root.iter(f"{_A}p"):
            text = "".join(t.text or "" for t in p.iter(f"{_A}t")).strip()
            if text:
                out.append(text)
        out.append("")
    return "\n".join(out)


def office_to_text(filename: str, data: bytes) -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    z = _Zip(data)
    try:
        if suffix == ".docx":
            return _docx_text(z)
        if suffix == ".xlsx":
            return _xlsx_text(z)
        if suffix == ".pptx":
            return _pptx_text(z)
    except ET.ParseError as e:
        raise PolicyError(f"could not parse {filename}: {e}") from e
    raise PolicyError(f"no text extractor for {suffix}")


# --- Read: save a downloaded attachment where the agent can read it -------------------------------


@dataclass(frozen=True)
class SavedAttachment:
    attachment_id: str
    filename: str
    path: str
    """Workspace-relative path of what was written, e.g. scratch/jira-attachments/PROJ-1/brd.docx.txt."""
    kind: str
    """"text" (decoded + redacted), "extracted" (Office -> redacted text) or "binary" (PDF/image, as sent)."""
    size: int
    redacted: bool
    how_to_read: str


def is_readable(filename: str) -> bool:
    return PurePosixPath(filename).suffix.lower() in READABLE_SUFFIXES


def save_attachment(workspace_root: Path, key: str, attachment_id: str, filename: str, data: bytes) -> SavedAttachment:
    require_issue_key(key)
    name = safe_filename(filename)
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in READABLE_SUFFIXES:
        raise PolicyError(f"{filename}: {suffix or 'no extension'} has no reader here; ask for a PDF, CSV or Office export")
    folder = workspace_root.joinpath(*DOWNLOAD_DIR.parts, key)
    folder.mkdir(parents=True, exist_ok=True)
    # Prefix with the attachment id so two attachments with one filename never overwrite each other.
    stem = f"{attachment_id}-{name}"
    if suffix in TEXT_SUFFIXES:
        raw = data.decode("utf-8", errors="replace")
        text = redact_secrets(raw)
        target, kind, out = folder / stem, "text", text.encode("utf-8")
        redacted = text != raw
        how = "Read it with the Read tool."
    elif suffix in OFFICE_SUFFIXES:
        raw = office_to_text(name, data)
        text = redact_secrets(raw)
        target, kind, out = folder / f"{stem}.txt", "extracted", text.encode("utf-8")
        redacted = text != raw
        how = ("Read it with the Read tool. Text was extracted from the Office file: layout, images, "
               "charts and formulas are not included; say so if they matter.")
    else:
        target, kind, out, redacted = folder / stem, "binary", data, False
        how = "Read it with the Read tool (it renders PDFs and images). Not secret-redacted: don't copy credentials from it."
    target.write_bytes(out)
    rel = target.relative_to(workspace_root).as_posix()
    return SavedAttachment(attachment_id=str(attachment_id), filename=filename, path=rel, kind=kind,
                           size=len(out), redacted=redacted, how_to_read=how)


# --- Write: what may be attached to a ticket ------------------------------------------------------


@dataclass(frozen=True)
class UploadFile:
    filename: str
    data: bytes
    content_type: str
    redacted: bool
    source: str


def prepare_upload(workspace_root: Path, file_path: str, filename: str | None = None) -> UploadFile:
    """Check and load a file for `jira-attach`. Refuses anything outside Scotri's own output and the
    caller's uploads, any format the upload policy doesn't accept, and anything over the size cap."""
    root = workspace_root.resolve()
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as e:
        raise NotFoundError(f"no such file: {file_path}") from e
    try:
        rel = PurePosixPath(resolved.relative_to(root).as_posix())
    except ValueError as e:
        raise PolicyError(f"refused: {file_path} is outside this workspace") from e
    if len(rel.parts) < 2 or rel.parts[0] not in UPLOAD_ROOTS:
        raise PolicyError(f"refused: only files under {', '.join(r + '/' for r in UPLOAD_ROOTS)} can be "
                          f"attached (Scotri's own deliverables and the caller's uploads), not {rel}")
    if rel.parts[:2] == DOWNLOAD_DIR.parts:
        raise PolicyError("refused: that file was downloaded from Jira; it is already on a ticket")
    if not resolved.is_file():
        raise PolicyError(f"refused: {rel} is not a regular file")
    name = safe_filename(filename or resolved.name)
    suffix = PurePosixPath(name).suffix.lower()
    if is_blocked(name):
        raise PolicyError(f"refused: {name} holds credentials and can't be attached")
    if UPLOAD_SUFFIXES is not None and suffix not in UPLOAD_SUFFIXES:
        raise PolicyError(f"refused: {suffix or 'no extension'} is not an allowed attachment type "
                          f"({', '.join(sorted(UPLOAD_SUFFIXES))})")
    size = resolved.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise PolicyError(f"refused: {rel} is {size} bytes; the cap is {MAX_UPLOAD_BYTES}")
    if size == 0:
        raise PolicyError(f"refused: {rel} is empty")
    data = resolved.read_bytes()
    redacted = False
    if suffix in TEXT_SUFFIXES:
        raw = data.decode("utf-8", errors="replace")
        text = redact_secrets(raw)
        redacted = text != raw
        data = text.encode("utf-8")
    return UploadFile(filename=name, data=data, content_type=content_type_for(name), redacted=redacted,
                      source=rel.as_posix())
