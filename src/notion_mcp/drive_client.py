"""Google Drive client for the optional `drive:` file scheme.

Access tokens come from one of two places (see config.py):

Broker mode (GOOGLE_AUTH_URL set): a google-auth token broker hands out
short-lived access tokens over HTTP. No client secret or refresh token lives
here, and BrokerCredentials.refresh() asks the broker again whenever the
cached Drive service needs a new token — so a re-authorised account works
without a restart.

Local mode (GOOGLE_AUTH_URL unset): the companion google-accounts-mcp server
persists refresh tokens in a SQLite database keyed by email. We open that DB
in SQLite read-only mode (`mode=ro`) so there is zero chance of clobbering
the companion server's state, and mint access tokens in memory — nothing is
written back.

Scope note: the tokens carry `drive.file`, which grants per-app access to
files the app created. Both modes use the same OAuth client as
google-accounts-mcp, so Google treats them as the same app — files uploaded
via google-accounts-mcp's drive_upload are readable/writable here, and vice
versa.
"""
import io
import mimetypes
import sqlite3
from datetime import UTC, datetime

from google.auth.credentials import Credentials as BaseCredentials
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from notion_mcp.broker_client import BrokerClient, BrokerError
from notion_mcp.config import (
    DRIVE_ACCOUNT,
    DRIVE_FOLDER_NAME,
    GOOGLE_AUTH_BEARER,
    GOOGLE_AUTH_URL,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_TOKEN_DB_PATH,
)


# googleapiclient adds ~8 MiB to the process and most sessions never touch Drive, so it
# loads on first use. The names match what they replace (tests patch `build`).
def build(*args, **kwargs):
    from googleapiclient.discovery import build as _build

    return _build(*args, **kwargs)


def _http_error():
    """googleapiclient's HttpError, for `except _http_error() as e:` (evaluated only once
    an exception is in flight)."""
    from googleapiclient.errors import HttpError

    return HttpError


def MediaInMemoryUpload(*args, **kwargs):
    from googleapiclient.http import MediaInMemoryUpload as _Upload

    return _Upload(*args, **kwargs)


def MediaIoBaseDownload(*args, **kwargs):
    from googleapiclient.http import MediaIoBaseDownload as _Download

    return _Download(*args, **kwargs)

# google-auth treats a token as expired 225 s before its expiry; a smaller
# min_ttl would make the broker client hand back the same cached token.
BROKER_MIN_TTL = 300
DRIVE_SCOPE = "drive.file"


class DriveAuthError(ValueError):
    """Drive auth failed in a way the user can act on."""


def _broker_error_message(e: BrokerError, account: str) -> str:
    if e.code == "reauth_required":
        return (
            f"Google rejected the stored authorisation for '{account}'. "
            "Re-authorise it in the google-auth UI (Reissue token), then "
            "retry — no restart needed."
        )
    if e.code == "forbidden":
        return (
            f"'{account}' is not shared with notion-mcp in google-auth. "
            "Grant it to the notion-mcp service in the google-auth UI."
        )
    if e.code == "insufficient_scope":
        missing = ", ".join(e.detail.get("missing") or []) or DRIVE_SCOPE
        return (
            f"'{account}' lacks a Google scope Drive needs ({missing}). "
            "Add it to the grant / re-authorise the account in the "
            "google-auth UI."
        )
    if e.code == "unknown_account":
        return f"google-auth has no account '{account}'. Add it in the google-auth UI."
    if e.code == "unauthorized":
        return "The google-auth broker rejected GOOGLE_AUTH_BEARER."
    return (
        f"google-auth broker unavailable at {GOOGLE_AUTH_URL} "
        f"({e.message}). This is usually transient — retry."
    )


class BrokerCredentials(BaseCredentials):
    """google-auth credentials whose refresh() asks the token broker."""

    def __init__(self, client: BrokerClient, account: str):
        super().__init__()
        self._client = client
        self._account = account

    def refresh(self, request):
        # Refresh of a token google-auth still considers valid means the
        # API answered 401: bypass the client cache.
        force = self.token is not None and not self.expired
        try:
            info = self._client.token_info(self._account, DRIVE_SCOPE, force=force)
        except BrokerError as e:
            raise DriveAuthError(_broker_error_message(e, self._account)) from e
        self.token = info["access_token"]
        # google-auth compares against a naive UTC datetime.
        self.expiry = datetime.fromtimestamp(
            info["expires_at"], UTC
        ).replace(tzinfo=None)


class DriveClient:
    def __init__(self):
        self._service = None
        self._folder_id: str | None = None

    def _read_refresh_token(self, email: str) -> str:
        # immutable=1 is required: the token-DB dir is a read-only bind mount,
        # and sqlite otherwise tries to create a rollback journal file in
        # the DB's directory even for mode=ro, which fails with
        # "unable to open database file".
        uri = f"file:{GOOGLE_TOKEN_DB_PATH}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute(
                "SELECT refresh_token FROM accounts WHERE email = ?", (email,)
            ).fetchone()
        if not row:
            raise ValueError(
                f"No refresh token for '{email}' in the companion token DB "
                f"({GOOGLE_TOKEN_DB_PATH}). Authorize it via "
                "google-accounts-mcp first."
            )
        return row[0]

    def _broker_credentials(self) -> BrokerCredentials:
        if not GOOGLE_AUTH_BEARER:
            raise DriveAuthError(
                "GOOGLE_AUTH_URL is set but GOOGLE_AUTH_BEARER is not. Set "
                "this service's google-auth bearer token."
            )
        client = BrokerClient(
            GOOGLE_AUTH_URL, GOOGLE_AUTH_BEARER, min_ttl=BROKER_MIN_TTL
        )
        return BrokerCredentials(client, DRIVE_ACCOUNT)

    def _local_credentials(self) -> Credentials:
        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            raise ValueError(
                "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set. These must "
                "match the client that google-accounts-mcp was authorized "
                "against."
            )
        refresh_token = self._read_refresh_token(DRIVE_ACCOUNT)
        # Intentionally no `scopes=` — Google rejects refresh calls that
        # request a wider scope than the refresh token was granted. Without
        # it we get an access token scoped to whatever the refresh token
        # originally received (matches google-accounts-mcp's own auth.py).
        return Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
        )

    def _get_service(self):
        if self._service is not None:
            return self._service
        if not DRIVE_ACCOUNT:
            raise ValueError(
                "DRIVE_ACCOUNT is not configured. The drive: scheme is an "
                "optional feature — set DRIVE_ACCOUNT plus either "
                "GOOGLE_AUTH_URL + GOOGLE_AUTH_BEARER (token broker) or "
                "GOOGLE_TOKEN_DB_PATH + the GOOGLE_CLIENT_* vars (companion "
                "google-accounts-mcp token DB) to enable it."
            )
        if GOOGLE_AUTH_URL:
            creds = self._broker_credentials()
        else:
            creds = self._local_credentials()
        creds.refresh(Request())
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def _get_folder_id(self) -> str:
        if self._folder_id:
            return self._folder_id
        service = self._get_service()
        escaped = DRIVE_FOLDER_NAME.replace("\\", "\\\\").replace("'", "\\'")
        q = (
            f"name = '{escaped}' "
            f"and mimeType = 'application/vnd.google-apps.folder' "
            f"and trashed = false"
        )
        res = service.files().list(
            q=q, spaces="drive", fields="files(id, name)", pageSize=10
        ).execute()
        files = res.get("files", [])
        if files:
            self._folder_id = files[0]["id"]
        else:
            meta = {
                "name": DRIVE_FOLDER_NAME,
                "mimeType": "application/vnd.google-apps.folder",
            }
            folder = service.files().create(body=meta, fields="id").execute()
            self._folder_id = folder["id"]
        return self._folder_id

    # --- Public API ------------------------------------------------------

    def list_files(self, filter: str = "") -> list[dict]:
        service = self._get_service()
        folder_id = self._get_folder_id()
        res = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="files(id, name, size, mimeType, modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=200,
        ).execute()
        files = res.get("files", [])
        if filter:
            files = [f for f in files if filter.lower() in f["name"].lower()]
        return files

    def upload_bytes(self, filename: str, data: bytes, mime: str | None = None) -> dict:
        service = self._get_service()
        folder_id = self._get_folder_id()
        if not mime:
            mime, _ = mimetypes.guess_type(filename)
        media = MediaInMemoryUpload(
            data, mimetype=mime or "application/octet-stream", resumable=False
        )
        meta = {"name": filename, "parents": [folder_id]}
        return service.files().create(
            body=meta, media_body=media,
            fields="id, name, size, mimeType, webViewLink",
        ).execute()

    def download_bytes(self, name_or_id: str) -> tuple[str, str, bytes]:
        """Returns (filename, mime_type, content_bytes)."""
        file_id, name, mime = self._resolve(name_or_id)
        if mime.startswith("application/vnd.google-apps"):
            raise ValueError(
                f"Drive file '{name}' has Google-native type '{mime}' — export "
                f"it to PDF/etc. in Drive first before pushing to Notion."
            )
        service = self._get_service()
        request = service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return name, mime, buf.getvalue()

    def _resolve(self, name_or_id: str) -> tuple[str, str, str]:
        if not name_or_id:
            raise ValueError("Drive file name or ID required.")
        service = self._get_service()
        folder_id = self._get_folder_id()
        escaped = name_or_id.replace("\\", "\\\\").replace("'", "\\'")
        res = service.files().list(
            q=f"'{folder_id}' in parents and name = '{escaped}' and trashed = false",
            fields="files(id, name, mimeType)",
            pageSize=10,
        ).execute()
        files = res.get("files", [])
        if len(files) == 1:
            f = files[0]
            return f["id"], f["name"], f.get("mimeType", "")
        if len(files) > 1:
            raise ValueError(
                f"Ambiguous Drive file '{name_or_id}' — {len(files)} matches "
                f"in folder '{DRIVE_FOLDER_NAME}'. Pass the file ID instead."
            )
        try:
            f = service.files().get(
                fileId=name_or_id, fields="id, name, mimeType, parents"
            ).execute()
        except _http_error() as e:
            if e.resp.status != 404:
                raise
            raise ValueError(f"Drive file '{name_or_id}' not found.") from e
        if folder_id not in (f.get("parents") or []):
            raise ValueError(
                f"Drive file '{name_or_id}' is not inside folder "
                f"'{DRIVE_FOLDER_NAME}'."
            )
        return f["id"], f["name"], f.get("mimeType", "")
