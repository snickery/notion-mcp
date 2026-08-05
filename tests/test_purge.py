"""Unit tests for notion_purge_shared_files."""
import os
import time

import pytest


@pytest.fixture
def tmp_shared_dir(tmp_path, monkeypatch):
    d = tmp_path / "shared"
    d.mkdir()
    monkeypatch.setenv("SHARED_DIR", str(d))
    monkeypatch.setattr("notion_mcp.config.SHARED_DIR", str(d))
    import notion_mcp.server as server
    monkeypatch.setattr(server, "SHARED_DIR", str(d))
    return d


def _age(path, hours):
    """Force ``path`` mtime to be ``hours`` ago."""
    t = time.time() - hours * 3600
    os.utime(path, (t, t))


def test_deletes_only_old_files(tmp_shared_dir):
    from notion_mcp.server import notion_purge_shared_files
    old = tmp_shared_dir / "old.pdf"
    new = tmp_shared_dir / "new.pdf"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    _age(old, 48)

    out = notion_purge_shared_files(max_age_hours=24)
    assert not old.exists()
    assert new.exists()
    assert "old.pdf" in out
    assert "Deleted" in out


def test_dry_run_lists_without_deleting(tmp_shared_dir):
    from notion_mcp.server import notion_purge_shared_files
    stale = tmp_shared_dir / "stale.pdf"
    stale.write_bytes(b"x")
    _age(stale, 48)

    out = notion_purge_shared_files(max_age_hours=24, dry_run=True)
    assert stale.exists()
    assert "Would delete" in out
    assert "stale.pdf" in out


def test_no_old_files(tmp_shared_dir):
    from notion_mcp.server import notion_purge_shared_files
    (tmp_shared_dir / "fresh.pdf").write_bytes(b"y")
    out = notion_purge_shared_files(max_age_hours=24)
    assert "No files older" in out


def test_rejects_negative_age(tmp_shared_dir):
    from notion_mcp.server import notion_purge_shared_files
    with pytest.raises(ValueError, match=">="):
        notion_purge_shared_files(max_age_hours=-1)


def test_leaves_subdirs_alone(tmp_shared_dir):
    from notion_mcp.server import notion_purge_shared_files
    sub = tmp_shared_dir / "subdir"
    sub.mkdir()
    (sub / "child.txt").write_bytes(b"x")
    _age(sub, 48)
    out = notion_purge_shared_files(max_age_hours=24)
    assert sub.exists()
    assert (sub / "child.txt").exists()
    assert "No files older" in out


def test_custom_max_age(tmp_shared_dir):
    from notion_mcp.server import notion_purge_shared_files
    recent = tmp_shared_dir / "recent.pdf"
    recent.write_bytes(b"z")
    _age(recent, 2)  # 2h old
    # 1h cutoff should sweep it up.
    out = notion_purge_shared_files(max_age_hours=1)
    assert not recent.exists()
    assert "recent.pdf" in out
