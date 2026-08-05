"""Unit tests for _read_source scheme parsing.

These tests cover the `local:`, `shared:`, and `base64:` source schemes
used by notion_upload_file / notion_add_file_to_row. The `drive:` scheme
is exercised by test_drive_client.py — it needs live-ish Google Drive
plumbing that is out of scope here.
"""
import base64

import pytest


@pytest.fixture
def tmp_files_dir(tmp_path, monkeypatch):
    d = tmp_path / "files"
    d.mkdir()
    monkeypatch.setenv("FILES_DIR", str(d))
    monkeypatch.setattr("notion_mcp.config.FILES_DIR", str(d))
    import notion_mcp.server as server
    monkeypatch.setattr(server, "FILES_DIR", str(d))
    return d


@pytest.fixture
def tmp_shared_dir(tmp_path, monkeypatch):
    d = tmp_path / "shared"
    d.mkdir()
    monkeypatch.setenv("SHARED_DIR", str(d))
    monkeypatch.setattr("notion_mcp.config.SHARED_DIR", str(d))
    import notion_mcp.server as server
    monkeypatch.setattr(server, "SHARED_DIR", str(d))
    return d


def test_local_scheme_reads_files_dir(tmp_files_dir):
    from notion_mcp.server import _read_source
    (tmp_files_dir / "a.txt").write_bytes(b"hello-local")
    data, name, mime = _read_source("local:a.txt")
    assert data == b"hello-local"
    assert name == "a.txt"
    assert mime == "text/plain"


def test_shared_scheme_reads_shared_dir(tmp_shared_dir):
    from notion_mcp.server import _read_source
    (tmp_shared_dir / "invoice.pdf").write_bytes(b"PDF-BYTES")
    data, name, mime = _read_source("shared:invoice.pdf")
    assert data == b"PDF-BYTES"
    assert name == "invoice.pdf"
    assert mime == "application/pdf"


def test_shared_scheme_missing_file(tmp_shared_dir):
    from notion_mcp.server import _read_source
    with pytest.raises(ValueError, match="Shared file not found"):
        _read_source("shared:ghost.pdf")


def test_shared_scheme_traversal_rejected(tmp_shared_dir, tmp_path):
    """A `shared:../something` must not escape SHARED_DIR."""
    (tmp_path / "outside.pdf").write_bytes(b"evil")
    from notion_mcp.server import _read_source
    with pytest.raises(ValueError, match="escapes"):
        _read_source("shared:../outside.pdf")


def test_local_scheme_traversal_rejected(tmp_files_dir, tmp_path):
    (tmp_path / "outside.pdf").write_bytes(b"evil")
    from notion_mcp.server import _read_source
    with pytest.raises(ValueError, match="escapes"):
        _read_source("local:../outside.pdf")


def test_base64_scheme_still_works():
    from notion_mcp.server import _read_source
    payload = base64.b64encode(b"tiny-bytes").decode()
    data, name, mime = _read_source(f"base64:{payload}", filename="tiny.bin")
    assert data == b"tiny-bytes"
    assert name == "tiny.bin"


def test_unknown_scheme_raises():
    from notion_mcp.server import _read_source
    with pytest.raises(ValueError, match="Unknown source scheme"):
        _read_source("sftp:example.com/x")


def test_missing_prefix_mentions_shared():
    """The error on a prefix-less source should list 'shared:' so agents
    discover the new scheme from the error message alone."""
    from notion_mcp.server import _read_source
    with pytest.raises(ValueError, match="shared"):
        _read_source("no-prefix-here")


# ---------------------------------------------------------------------------
# notion_list_local_files: location parameter
# ---------------------------------------------------------------------------

def test_list_local_files_default_lists_files_dir(tmp_files_dir, tmp_shared_dir):
    """Default location='files' lists FILES_DIR, not SHARED_DIR."""
    (tmp_files_dir / "in-files.pdf").write_bytes(b"a")
    (tmp_shared_dir / "in-shared.pdf").write_bytes(b"b")
    from notion_mcp.server import notion_list_local_files
    out = notion_list_local_files()
    assert "in-files.pdf" in out
    assert "in-shared.pdf" not in out
    assert "notion-mcp local store" in out
    assert "[local:in-files.pdf]" in out


def test_list_local_files_shared_location(tmp_files_dir, tmp_shared_dir):
    """location='shared' lists SHARED_DIR and emits the shared: reference."""
    (tmp_files_dir / "in-files.pdf").write_bytes(b"a")
    (tmp_shared_dir / "in-shared.pdf").write_bytes(b"b")
    from notion_mcp.server import notion_list_local_files
    out = notion_list_local_files(location="shared")
    assert "in-shared.pdf" in out
    assert "in-files.pdf" not in out
    assert "shared cross-MCP mount" in out
    assert "[shared:in-shared.pdf]" in out


def test_list_local_files_shared_empty(tmp_shared_dir):
    """Empty shared mount returns an explanatory message, not a crash."""
    from notion_mcp.server import notion_list_local_files
    out = notion_list_local_files(location="shared")
    assert "empty" in out
    assert "shared cross-MCP mount" in out


def test_list_local_files_filter_applies_to_shared(tmp_shared_dir):
    """The filter substring applies regardless of which location is chosen."""
    (tmp_shared_dir / "keep-me.pdf").write_bytes(b"a")
    (tmp_shared_dir / "drop-me.pdf").write_bytes(b"b")
    from notion_mcp.server import notion_list_local_files
    out = notion_list_local_files(filter="keep", location="shared")
    assert "keep-me.pdf" in out
    assert "drop-me.pdf" not in out


def test_list_local_files_invalid_location_raises(tmp_shared_dir):
    from notion_mcp.server import notion_list_local_files
    with pytest.raises(ValueError, match="'files' or 'shared'"):
        notion_list_local_files(location="elsewhere")
