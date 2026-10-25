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
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from notion_mcp import drive_client
from notion_mcp.broker_client import BrokerError


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


# --- Broker mode ----------------------------------------------------------

class FakeBroker:
    def __init__(self, error: BrokerError | None = None):
        self.calls: list[tuple] = []
        self.error = error

    def token_info(self, account, scopes="", force=False):
        self.calls.append((account, scopes, force))
        if self.error:
            raise self.error
        return {
            "access_token": f"at-{len(self.calls)}",
            "expires_at": time.time() + 3600,
            "scopes": ["https://www.googleapis.com/auth/drive.file"],
        }


def test_broker_credentials_refresh_sets_token_and_expiry():
    broker = FakeBroker()
    creds = drive_client.BrokerCredentials(broker, "nick@example.com")
    assert not creds.valid
    creds.refresh(None)
    assert creds.token == "at-1"
    assert creds.valid
    assert creds.expiry.tzinfo is None, "google-auth compares naive UTC"
    assert broker.calls == [("nick@example.com", "drive.file", False)]


def test_broker_credentials_refresh_of_valid_token_bypasses_cache():
    """google-auth only re-refreshes a still-valid token after an API 401,
    so the client cache must be bypassed."""
    broker = FakeBroker()
    creds = drive_client.BrokerCredentials(broker, "nick@example.com")
    creds.refresh(None)
    creds.refresh(None)
    assert broker.calls[1][2] is True
    assert creds.token == "at-2"


@pytest.mark.parametrize(
    "code, expected",
    [
        ("reauth_required", "Re-authorise it in the google-auth UI"),
        ("forbidden", "not shared with notion-mcp"),
        ("insufficient_scope", "drive.file"),
        ("unknown_account", "has no account"),
        ("unauthorized", "GOOGLE_AUTH_BEARER"),
        ("unavailable", "retry"),
    ],
)
def test_broker_errors_become_clear_tool_errors(code, expected):
    broker = FakeBroker(error=BrokerError(code, "boom"))
    creds = drive_client.BrokerCredentials(broker, "nick@example.com")
    with pytest.raises(drive_client.DriveAuthError, match=expected):
        creds.refresh(None)


def test_get_service_uses_broker_when_url_set():
    """Broker mode must not touch the token DB or need the client secret."""
    built: dict = {}

    def fake_build(*args, credentials, **kwargs):
        built["credentials"] = credentials
        return object()

    with patch.object(drive_client, "GOOGLE_AUTH_URL", "http://broker:8335"), \
         patch.object(drive_client, "GOOGLE_AUTH_BEARER", "b"), \
         patch.object(drive_client, "DRIVE_ACCOUNT", "nick@example.com"), \
         patch.object(drive_client, "GOOGLE_CLIENT_SECRET", ""), \
         patch.object(drive_client, "GOOGLE_TOKEN_DB_PATH", "/nonexistent/tokens.db"), \
         patch.object(drive_client.BrokerClient, "token_info",
                      lambda self, account, scopes="", force=False: {
                          "access_token": "at", "expires_at": time.time() + 3600}), \
         patch.object(drive_client, "build", side_effect=fake_build):
        drive_client.DriveClient()._get_service()

    assert isinstance(built["credentials"], drive_client.BrokerCredentials)
    assert built["credentials"].token == "at"


def test_broker_mode_without_bearer_is_a_clear_error():
    with patch.object(drive_client, "GOOGLE_AUTH_URL", "http://broker:8335"), \
         patch.object(drive_client, "GOOGLE_AUTH_BEARER", ""), \
         patch.object(drive_client, "DRIVE_ACCOUNT", "nick@example.com"), \
         pytest.raises(drive_client.DriveAuthError, match="GOOGLE_AUTH_BEARER"):
        drive_client.DriveClient()._get_service()
