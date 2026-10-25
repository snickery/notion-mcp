import os

# --- Notion --------------------------------------------------------------
# Internal integration token from https://www.notion.so/my-integrations
# (starts with `ntn_` or `secret_`). Used directly as the API bearer.
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_API_VERSION = os.environ.get("NOTION_API_VERSION", "2026-03-11")
NOTION_API_BASE = "https://api.notion.com/v1"

# --- Google Drive (optional feature) --------------------------------------
# Two ways to get a Drive access token for DRIVE_ACCOUNT:
#
# 1. Broker mode (GOOGLE_AUTH_URL set): ask a google-auth token broker
#    over HTTP. This server then holds no OAuth client secret and no
#    refresh token, and a re-authorised account is picked up without a
#    restart.
# 2. Local mode (GOOGLE_AUTH_URL unset): read the refresh token from the
#    companion google-accounts-mcp server's sqlite token DB (opened
#    read-only, never written back) and exchange it with GOOGLE_CLIENT_*.
#
# Skip all DRIVE_/GOOGLE_ vars to run without Drive.
GOOGLE_AUTH_URL = os.environ.get("GOOGLE_AUTH_URL", "").rstrip("/")
GOOGLE_AUTH_BEARER = os.environ.get("GOOGLE_AUTH_BEARER", "")
GOOGLE_TOKEN_DB_PATH = (
    os.environ.get("GOOGLE_TOKEN_DB_PATH")
    or os.environ.get("GMAIL_TOKEN_DB_PATH")  # legacy name, still honored
    or "/google-data/tokens.db"
)
# Account whose Drive hosts the shared folder. Required only for drive: sources.
DRIVE_ACCOUNT = os.environ.get("DRIVE_ACCOUNT", "")
DRIVE_FOLDER_NAME = os.environ.get("DRIVE_FOLDER_NAME", "mcp-google-accounts")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

# --- Local file store ----------------------------------------------------
FILES_DIR = os.environ.get("FILES_DIR", "/data/files")
# Shared mount exposed by both this server and the companion
# google-accounts-mcp. Files dropped there (destination='shared') are
# instantly readable here, letting the cross-MCP attachment pipeline
# skip base64 entirely.
SHARED_DIR = os.environ.get("SHARED_DIR", "/shared")

# --- Server --------------------------------------------------------------
PORT = int(os.environ.get("PORT", "8322"))
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
