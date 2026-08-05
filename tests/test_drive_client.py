"""Unit tests for drive_client.py.

The main regression we guard against: sqlite opening the gmail-mcp tokens
DB from a read-only bind mount must use immutable=1, otherwise sqlite
tries to create a rollback journal in the (read-only) directory and
fails with "unable to open database file". We can't fully simulate a
ro bind mount inside a unit test (chmod gives EACCES, not EROFS, and
sqlite handles them differently), so we assert the URI string itself
— the only place the flag can come from.
"""
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from notion_mcp import drive_client


@pytest.fixture
def tokens_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "tokens.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE accounts (email TEXT PRIMARY KEY, refresh_token TEXT)")
    conn.execute(
        "INSERT INTO accounts VALUES (?, ?)", ("nick@example.com", "rt-abc"),
    )
    conn.commit()
    conn.close()
    return db_path


def test_sqlite_uri_contains_immutable_flag(tokens_db: Path):
    """Regression: the sqlite URI must include both mode=ro and immutable=1.
    Without immutable=1, sqlite attempts to create a rollback journal in
    the DB's directory, which fails on the read-only token-DB bind mount
    with 'unable to open database file'."""
    real_connect = sqlite3.connect
    captured: dict = {}

    def spy(database, *args, **kwargs):
        captured["uri"] = database
        captured["uri_flag"] = kwargs.get("uri")
        return real_connect(database, *args, **kwargs)

    with patch.object(drive_client, "GOOGLE_TOKEN_DB_PATH", str(tokens_db)), \
         patch.object(drive_client.sqlite3, "connect", side_effect=spy):
        client = drive_client.DriveClient()
        token = client._read_refresh_token("nick@example.com")

    assert token == "rt-abc"
    assert captured["uri_flag"] is True, "must open with uri=True"
    uri = captured["uri"]
    assert uri.startswith(f"file:{tokens_db}")
    assert "mode=ro" in uri
    assert "immutable=1" in uri, (
        "missing immutable=1 — sqlite will fail on read-only bind mounts"
    )


def test_read_refresh_token_returns_value(tokens_db: Path):
    with patch.object(drive_client, "GOOGLE_TOKEN_DB_PATH", str(tokens_db)):
        client = drive_client.DriveClient()
        assert client._read_refresh_token("nick@example.com") == "rt-abc"


def test_read_refresh_token_missing_email(tokens_db: Path):
    with patch.object(drive_client, "GOOGLE_TOKEN_DB_PATH", str(tokens_db)):
        client = drive_client.DriveClient()
        with pytest.raises(ValueError, match="No refresh token"):
            client._read_refresh_token("ghost@example.com")
