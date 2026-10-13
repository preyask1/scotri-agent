# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""OPEN_ITEMS.md #6(f): uploading a file over MCP, then attaching it with scotri_start."""
from __future__ import annotations

import base64
import types

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from adapters.artifact_store.local_fs_adapter import LocalFsArtifactStore
from domain.errors import PolicyError
from domain.models import Principal
from domain.shared_files import UploadPolicy, decode_upload_content, resolve_uploads

ALICE = Principal(subject="alice", display_name="Alice", platform="test", roles=[])


def test_text_and_base64_decode():
    assert decode_upload_content("héllo", "text", 100) == "héllo".encode()
    raw = bytes(range(256))
    assert decode_upload_content(base64.b64encode(raw).decode(), "base64", 1000) == raw
    wrapped = base64.encodebytes(raw).decode()  # with newlines, as many encoders emit
    assert decode_upload_content(wrapped, "base64", 1000) == raw


@pytest.mark.parametrize("content,encoding,limit", [
    ("x" * 11, "text", 10),
    (base64.b64encode(b"x" * 100).decode(), "base64", 10),  # refused before decoding
    ("not base64!", "base64", 100),
    ("abc", "hex", 100),
])
def test_bad_or_oversized_content_is_refused(content, encoding, limit):
    with pytest.raises(PolicyError):
        decode_upload_content(content, encoding, limit)


@pytest.fixture
def mcp(monkeypatch, tmp_path):
    import app.mcp_server as mcp_server

    store = LocalFsArtifactStore(tmp_path / "store")
    fake = types.SimpleNamespace(artifact_store=store, upload_policy=UploadPolicy(max_bytes=1000))
    monkeypatch.setattr(mcp_server, "container", fake)
    monkeypatch.setattr(mcp_server, "_authenticate", lambda ctx: ALICE)
    return mcp_server, store


def test_upload_stores_a_redacted_file_the_caller_can_attach(mcp):
    mcp_server, store = mcp
    out = mcp_server.scotri_upload("notes.md", "see password=hunter2222 here", None)
    assert out["filename"] == "notes.md" and out["redacted"] is True
    [record] = resolve_uploads(store, [out["upload_id"]], "alice")
    assert b"hunter2222" not in store.get(record.key)[0]
    with pytest.raises(PolicyError):
        resolve_uploads(store, [out["upload_id"]], "bob")


def test_upload_accepts_base64_binary(mcp):
    mcp_server, _ = mcp
    pdf = b"%PDF-1.4\n\x00\xff"
    out = mcp_server.scotri_upload("scan.pdf", base64.b64encode(pdf).decode(), None, encoding="base64")
    assert out["size"] == len(pdf) and out["content_type"] == "application/pdf"


@pytest.mark.parametrize("name,content", [(".env", "A=1"), ("big.txt", "x" * 2000), ("empty.md", "")])
def test_upload_refusals_reach_the_client_as_tool_errors(mcp, name, content):
    mcp_server, _ = mcp
    with pytest.raises(ToolError):
        mcp_server.scotri_upload(name, content, None)
