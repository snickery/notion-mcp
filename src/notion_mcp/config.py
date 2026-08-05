import os

# --- Notion --------------------------------------------------------------
# Internal integration token from https://www.notion.so/my-integrations
# (starts with `ntn_` or `secret_`). Used directly as the API bearer.
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_API_VERSION = os.environ.get("NOTION_API_VERSION", "2026-03-11")
NOTION_API_BASE = "https://api.notion.com/v1"

# --- Google Drive (optional companion-server feature) ---------------------
# The drive: scheme reuses refresh tokens from the companion
# google-accounts-mcp server (formerly gmail-mcp), which stores them in a
# sqlite DB keyed by email. We mount that directory read-only and open the
# DB in read-only mode to mint short-lived Drive access tokens without ever
# writing back. Skip all GMAIL_/DRIVE_/GOOGLE_ vars to run without Drive.
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
