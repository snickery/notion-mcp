"""Google Drive client that reuses google-accounts-mcp's stored refresh tokens.

The companion google-accounts-mcp server persists refresh tokens in a SQLite database keyed by
email. We mount that directory into the notion-mcp container read-only and
open the DB in SQLite read-only mode (`mode=ro`) so there is zero chance of
clobbering the companion server's state. Access tokens are minted in-memory on demand —
the refresh exchange is a pure HTTP call to Google, nothing is written back.

Scope note: google-accounts-mcp requests `drive.file`, which grants per-app access to
files the app created. Because notion-mcp uses the same GOOGLE_CLIENT_ID,
Google treats it as the same app — so files uploaded via google-accounts-mcp's
drive_upload are readable/writable here, and vice versa.
"""
import base64
import io
import mimetypes
import sqlite3
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload

from notion_mcp.config import (
    DRIVE_ACCOUNT,
    DRIVE_FOLDER_NAME,
    GOOGLE_TOKEN_DB_PATH,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
)


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

    def _get_service(self):
        if self._service is not None:
            return self._service
        if not DRIVE_ACCOUNT:
            raise ValueError(
                "DRIVE_ACCOUNT is not configured. The drive: scheme is an "
                "optional feature backed by the companion google-accounts-mcp "
                "server's token DB — set GOOGLE_TOKEN_DB_PATH, DRIVE_ACCOUNT, "
                "and the GOOGLE_CLIENT_* vars to enable it."
            )
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
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
        )
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
        except HttpError as e:
            raise ValueError(f"Drive file '{name_or_id}' not found.") from e
        if folder_id not in (f.get("parents") or []):
            raise ValueError(
                f"Drive file '{name_or_id}' is not inside folder "
                f"'{DRIVE_FOLDER_NAME}'."
            )
        return f["id"], f["name"], f.get("mimeType", "")
