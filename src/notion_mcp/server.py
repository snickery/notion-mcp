import base64
import concurrent.futures
import hashlib
import json
import logging
import mimetypes
import os
import re
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from mcp_server_kit import offload_sync_tools, serve
from mcp_server_kit import pdf as pdf_read
from mcp_server_kit.files import atomic_create as _atomic_create

from notion_mcp.blocks import (
    blocks_to_markdown,
    format_page_properties,
    format_property_value,
    markdown_to_blocks,
    md_to_rich_text,
)
from notion_mcp.config import FILES_DIR, MCP_BEARER_TOKEN, PORT, SHARED_DIR
from notion_mcp.drive_client import DriveClient
from notion_mcp.notion_client import NotionClient, pick_block_type

INSTRUCTIONS = """\
Notion workspace server (pages, databases, blocks, comments, files).
File transfer uses unified source schemes: 'local:<path>' (private
store), 'shared:<filename>' (cross-MCP mount, shared with the
companion google-accounts-mcp server),
'drive:<name-or-id>' (shared Drive folder), 'base64:<bytes>' (small
files only). Zero-copy pipeline with google-accounts-mcp via the shared mount:
Gmail→Notion: gmail download_attachment(destination='shared') then
notion_upload_file(source='shared:<name>'). Notion→Gmail:
notion_download_file(destination='shared') then gmail
draft_email/send_email(attachments=['shared:<name>']). Bytes never
traverse MCP parameters that way — no size limits. Shared files may be
purged periodically (24h in the reference deployment); verify staging with
notion_list_local_files(location='shared') (returns sha256 for
integrity checks, as do all transfer tools).
PDFs are readable in place — no shared-mount hop needed just to read:
notion_extract_file_text(source='notion:<block_id>') gets the text
(scanned/image-only pages are OCR'd automatically; ocr='llm' routes
hard documents to a vision model), and notion_render_file_page returns
a viewable image of any page. Both accept notion:/shared:/local:/drive:
sources.
Writing: notion_append_content / notion_create_page convert markdown to
blocks locally; notion_insert_page_content, notion_replace_page_content
and notion_create_page(native_markdown=True) use Notion's own parser
(tables, callouts, columns, mentions). Pass silent=True on page writes
to avoid notifying people. A large native write may return a task ID —
check it with notion_get_async_task, never retry it blind.
Database rows can start from a template (notion_list_templates →
notion_create_page(template=...)). AI meeting notes:
notion_query_meeting_notes → notion_read_meeting_notes."""

mcp = FastMCP(
    "notion",
    instructions=INSTRUCTIONS,
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    json_response=True,
)
offload_sync_tools(mcp, logger_name="notion_mcp.requests")
notion = NotionClient()
drive = DriveClient()


def _safe_path_under(root_str: str, filename: str) -> Path:
    """Resolve a user-supplied path under ``root_str``, rejecting traversal."""
    root = Path(root_str).resolve()
    root.mkdir(parents=True, exist_ok=True)
    p = (root / filename).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"Path '{filename}' escapes {root_str}.")
    return p


def _safe_local_path(filename: str) -> Path:
    """Resolve a user-supplied path under FILES_DIR, rejecting traversal."""
    return _safe_path_under(FILES_DIR, filename)


def _safe_shared_path(filename: str) -> Path:
    """Resolve a user-supplied path under SHARED_DIR, rejecting traversal."""
    return _safe_path_under(SHARED_DIR, filename)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_source(source: str, filename: str = "") -> tuple[bytes, str, str]:
    """Parse a `scheme:value` source into (bytes, filename, mime_type)."""
    if ":" not in source:
        raise ValueError(
            "source must be prefixed: 'local:...', 'shared:...', "
            "'drive:...', 'notion:...', or 'base64:...'"
        )
    scheme, _, value = source.partition(":")
    scheme = scheme.strip().lower()

    if scheme == "notion":
        # A file/image/pdf/video/audio block already in the workspace
        # (block IDs come from notion_list_files_on_page).
        notion_name, mime, data = notion.fetch_file_block_bytes(value.strip())
        name = filename or notion_name
    elif scheme == "local":
        path = _safe_local_path(value)
        if not path.is_file():
            raise ValueError(f"Local file not found: {value}")
        data = path.read_bytes()
        name = filename or path.name
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    elif scheme == "shared":
        path = _safe_shared_path(value)
        if not path.is_file():
            raise ValueError(
                f"Shared file not found: {value} (expected under {SHARED_DIR})"
            )
        data = path.read_bytes()
        name = filename or path.name
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    elif scheme == "drive":
        drive_name, mime, data = drive.download_bytes(value)
        name = filename or drive_name
    elif scheme == "base64":
        if not filename:
            raise ValueError("filename is required for base64 sources.")
        try:
            data = base64.b64decode(value, validate=True)
        except Exception as e:
            raise ValueError(f"Invalid base64 content: {e}") from e
        name = filename
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    else:
        raise ValueError(f"Unknown source scheme '{scheme}'.")
    return data, name, mime


# Notion's single-part ceiling; larger payloads must use multi_part with
# 5–20 MB parts (final part may be smaller). 10 MiB is the documented
# sweet spot.
SINGLE_PART_MAX = 20 * 1024 * 1024
MULTI_PART_CHUNK = 10 * 1024 * 1024


def _upload_bytes_to_notion(data: bytes, name: str, mime: str) -> str:
    """Stage bytes in Notion and return the upload_id. Transparently
    switches to the multi_part protocol when the payload exceeds the
    20 MB single-part ceiling."""
    if len(data) <= SINGLE_PART_MAX:
        upload = notion.create_file_upload(name, mime)
        upload_id = upload.get("id")
        if not upload_id:
            raise RuntimeError(f"Notion create_file_upload returned no id: {upload}")
        notion.send_file_upload(upload_id, name, data, mime)
        return upload_id

    chunks = [
        data[i:i + MULTI_PART_CHUNK]
        for i in range(0, len(data), MULTI_PART_CHUNK)
    ]
    upload = notion.create_file_upload(
        name, mime, mode="multi_part", number_of_parts=len(chunks),
    )
    upload_id = upload.get("id")
    if not upload_id:
        raise RuntimeError(f"Notion create_file_upload returned no id: {upload}")
    for part_number, chunk in enumerate(chunks, start=1):
        notion.send_file_upload(
            upload_id, name, chunk, mime, part_number=part_number,
        )
    notion.complete_file_upload(upload_id)
    return upload_id


def _position_param(position: str) -> dict | None:
    """Map the LLM-friendly position string to the API's position object.

    'end' (default) → None (append), 'start' → insert first, anything
    else is treated as a block ID → insert directly after that block."""
    pos = (position or "end").strip()
    low = pos.lower()
    if low in ("", "end"):
        return None
    if low == "start":
        return {"type": "start"}
    return {"type": "after_block", "after_block": {"id": pos}}


def _new_block_id(api_response: dict) -> str:
    """Pull the created block's ID out of a PATCH /blocks/:id/children
    response."""
    results = api_response.get("results", [])
    return results[0].get("id", "(unknown)") if results else "(unknown)"


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$", re.IGNORECASE
)


def _icon_param(icon: str) -> dict | None:
    """Map the LLM-friendly icon string to the API's icon object.

    'remove' → None (clears it); 'custom_emoji:<name-or-id>' → a workspace
    custom emoji (names are resolved via the custom-emoji list);
    'icon:<name>[:<color>]' → a native Notion icon; an http(s) URL → an
    external image; anything else is taken as an emoji character."""
    icon = icon.strip()
    if icon == "remove":
        return None
    if icon.startswith("custom_emoji:"):
        ref = icon.split(":", 1)[1].strip().strip(":")
        if not _UUID_RE.match(ref):
            matches = notion.list_custom_emojis(name=ref)
            if not matches:
                raise ValueError(
                    f"No custom emoji named '{ref}'. See notion_list_custom_emojis."
                )
            ref = matches[0]["id"]
        return {"type": "custom_emoji", "custom_emoji": {"id": ref}}
    if icon.startswith("icon:"):
        name, _, color = icon.split(":", 1)[1].partition(":")
        native: dict = {"name": name.strip()}
        if color.strip():
            native["color"] = color.strip()
        return {"type": "icon", "icon": native}
    if icon.startswith(("http://", "https://")):
        return {"type": "external", "external": {"url": icon}}
    return {"type": "emoji", "emoji": icon}


def _pending_task_note(res: dict) -> str:
    """Text for a write whose async task was still running at the wait
    limit, or '' when the write finished."""
    if res.get("object") != "async_task":
        return ""
    return (
        f"Notion is still processing this write (task {res.get('id')}, "
        f"status {res.get('status')}). Check it with "
        f"notion_get_async_task before retrying — a retry would duplicate it."
    )


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------


@mcp.tool()
def notion_search_pages(query: str, limit: int = 10, in_trash: bool = False) -> str:
    """Search Notion for pages the integration has access to. Returns titles
    and page IDs — use these IDs with notion_upload_file / list_files_on_page.
    The integration only sees pages explicitly shared with it in Notion.
    in_trash=True searches trashed pages instead of active ones (restore
    matches with notion_restore_page)."""
    pages = notion.search_pages(query, page_size=limit, in_trash=in_trash)
    if not pages:
        where = " in the trash" if in_trash else ""
        return f"No pages found for '{query}'{where}."
    lines = [f"{len(pages)} page(s) matching '{query}':\n"]
    for p in pages:
        title = notion.get_page_title(p)
        lines.append(f"  • {title}")
        lines.append(f"      id: {p['id']}")
        lines.append(f"      url: {p.get('url', '')}")
    return "\n".join(lines)


@mcp.tool()
def notion_search_databases(query: str, limit: int = 10) -> str:
    """Search Notion for databases the integration has access to. Returns
    titles, IDs, and property schemas. Use the database ID with
    notion_query_database, notion_describe_database, or notion_create_page
    (with database_id).

    The integration only sees databases explicitly shared with it in Notion.
    """
    results = notion.search_databases(query, page_size=limit)
    if not results:
        return f"No databases found for '{query}'."
    lines = [f"{len(results)} database(s) matching '{query}':\n"]
    for ds in results:
        title = "".join(
            t.get("plain_text", "") for t in ds.get("title", [])
        ) or "(untitled)"
        # Search returns data_source objects in 2025-09-03+
        ds_id = ds["id"]
        parent = ds.get("parent", {})
        db_id = parent.get("database_id", ds_id)
        lines.append(f"  {title}")
        lines.append(f"      database_id: {db_id}")
        lines.append(f"      data_source_id: {ds_id}")
        lines.append(f"      url: {ds.get('url', '')}")
        prop_types = [
            f"{name} [{p['type']}]"
            for name, p in ds.get("properties", {}).items()
        ]
        if prop_types:
            lines.append(f"      properties: {', '.join(prop_types)}")
    return "\n".join(lines)


@mcp.tool()
def notion_list_files_on_page(page_id: str) -> str:
    """List all file/image/pdf/video/audio blocks on a Notion page. Returns
    block IDs and filenames — use block IDs with notion_download_file."""
    blocks = notion.list_file_blocks(page_id)
    if not blocks:
        return f"No file blocks on page {page_id}."
    lines = [f"{len(blocks)} file block(s) on page {page_id}:\n"]
    for b in blocks:
        name = b["name"] or "(untitled)"
        lines.append(f"  • {name}  [{b['block_type']}, source={b['source_type']}]")
        lines.append(f"      block_id: {b['block_id']}")
    return "\n".join(lines)


@mcp.tool()
def notion_purge_shared_files(
    max_age_hours: float = 24.0, dry_run: bool = False,
) -> str:
    """Delete files from the cross-MCP shared mount (SHARED_DIR, /shared
    inside the container) whose modification time is older than
    max_age_hours. Use this to clean up attachments handed over from
    the companion google-accounts-mcp server once they've been uploaded
    to Notion.

    A host-level TTL sweep (if you run one) makes this automatic; the
    tool is mainly for agents that want to clean up proactively at the
    end of a workflow without waiting for it.

    Set dry_run=True to list victims without deleting. Only regular
    files are touched — subdirectories are left alone."""
    import time
    if max_age_hours < 0:
        raise ValueError("max_age_hours must be >= 0")
    root = Path(SHARED_DIR)
    if not root.exists():
        return f"Shared dir {SHARED_DIR} does not exist (nothing to purge)."
    cutoff = time.time() - max_age_hours * 3600
    victims = [
        p for p in root.iterdir()
        if p.is_file() and p.stat().st_mtime < cutoff
    ]
    if not victims:
        return (
            f"No files older than {max_age_hours}h in {SHARED_DIR} "
            f"(dir contains {sum(1 for _ in root.iterdir())} entries)."
        )
    verb = "Would delete" if dry_run else "Deleted"
    lines = [
        f"{verb} {len(victims)} file(s) older than {max_age_hours}h "
        f"from {SHARED_DIR}:"
    ]
    for p in sorted(victims):
        age_h = (time.time() - p.stat().st_mtime) / 3600
        size = p.stat().st_size
        lines.append(f"  • {p.name}  ({size} bytes, age {age_h:.1f}h)")
        if not dry_run:
            p.unlink()
    return "\n".join(lines)


@mcp.tool()
def notion_purge_file(filename: str) -> str:
    """Delete a single file from the cross-MCP shared mount by bare
    filename. Use this after a successful upload to free the shared
    slot instead of waiting for the daily TTL sweep (or calling
    notion_purge_shared_files with max_age_hours=0, which would wipe
    any unrelated in-flight handoffs). Rejects any filename containing
    path separators."""
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name != filename:
        raise ValueError(
            f"Invalid filename '{filename}' — must be a bare filename."
        )
    target = Path(SHARED_DIR) / safe_name
    if not target.is_file():
        return f"No such file in shared mount: {safe_name}"
    size = target.stat().st_size
    target.unlink()
    return f"Deleted {safe_name} ({size} bytes) from {SHARED_DIR}."


@mcp.tool()
def notion_list_local_files(filter: str = "", location: str = "files") -> str:
    """List files available to notion-mcp by scheme, so the agent can
    verify a handoff before attaching it to Notion.

    location:
      • 'files'  (default) — the notion-mcp private store under FILES_DIR.
                 These are addressable as source='local:<path>'.
      • 'shared' — the cross-MCP shared mount under SHARED_DIR. Files
                 placed here by google-accounts-mcp (download_attachment with
                 destination='shared') are addressable as
                 source='shared:<filename>'.

    Optional substring filter on the filename."""
    loc = location.strip().lower()
    if loc == "files":
        root = Path(FILES_DIR)
        scheme = "local"
        label = "notion-mcp local store"
    elif loc == "shared":
        root = Path(SHARED_DIR)
        scheme = "shared"
        label = "shared cross-MCP mount"
    else:
        raise ValueError(
            f"location must be 'files' or 'shared', got {location!r}."
        )
    root.mkdir(parents=True, exist_ok=True)
    files = [p for p in root.rglob("*") if p.is_file()]
    if filter:
        files = [p for p in files if filter.lower() in p.name.lower()]
    if not files:
        suffix = f" matching '{filter}'" if filter else ""
        return f"{label} is empty{suffix}."
    lines = [f"{len(files)} file(s) in {label}:\n"]
    for p in sorted(files):
        rel = p.relative_to(root)
        data = p.read_bytes()
        lines.append(
            f"  • {rel}  ({len(data)} bytes, sha256 {_sha256(data)})  "
            f"[{scheme}:{rel}]"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@mcp.tool()
def notion_upload_file(
    source: str,
    parent_page_id: str,
    filename: str = "",
    caption: str = "",
    position: str = "end",
    embed_html: bool = False,
) -> str:
    """Upload a file to Notion and attach it as a new block.

    source accepts one of:
      • `local:<relative-path>`  — a file under the notion-mcp private
        store (FILES_DIR). List with notion_list_local_files.
      • `shared:<filename>`      — a file on the shared cross-MCP mount,
        written there by google-accounts-mcp download_attachment with
        destination='shared'. List with
        notion_list_local_files(location='shared').
      • `drive:<name-or-id>`     — a file in the shared Drive folder (DRIVE_FOLDER_NAME)
        Google Drive folder (read from google-accounts-mcp's credentials, read-only).
      • `base64:<bytes>`         — raw base64 content; requires filename.
        Subject to MCP parameter size limits — prefer `shared:` or `drive:`
        for anything over ~20KB.

    parent_page_id is a Notion page ID (find via notion_search_pages) OR
    any block ID that supports children (toggle, column, callout, ...) to
    attach the file inside that block.

    position controls where the new block lands among the parent's
    children: 'end' (default), 'start', or a block ID to insert directly
    AFTER that block (get block IDs from
    notion_read_page(include_block_ids=True) or notion_list_files_on_page).

    filename overrides the source filename when provided. caption is shown
    below the file block in Notion. Files larger than 20 MB are uploaded
    via Notion's multi-part protocol automatically. To swap the file in an
    existing block without moving it, use notion_replace_file instead.

    embed_html=True attaches an .html/.htm file as an embed block that
    Notion renders in the page, instead of a downloadable file block
    (caption is not applied to embeds).

    ## Cross-MCP file transfer
    To upload a Gmail attachment to a Notion page:
      1. google-accounts-mcp download_attachment(..., destination='shared')
      2. notion_upload_file(source='shared:<filename>', parent_page_id=...)
    Files never traverse MCP parameters — no size limits.
    """
    data, name, mime = _read_source(source, filename)
    if embed_html:
        if not name.lower().endswith((".html", ".htm")):
            raise ValueError("embed_html needs an .html or .htm file.")
        mime = "text/html"
        block_type = "embed"
        upload_id = _upload_bytes_to_notion(data, name, mime)
        resp = notion.attach_html_embed(
            parent_page_id, upload_id, position=_position_param(position),
        )
    else:
        block_type = pick_block_type(mime)
        upload_id = _upload_bytes_to_notion(data, name, mime)
        resp = notion.attach_file_to_page(
            parent_page_id, upload_id, name, caption=caption,
            block_type=block_type, position=_position_param(position),
        )

    return (
        f"Uploaded to Notion:\n"
        f"  Filename: {name}\n"
        f"  Size: {len(data)} bytes\n"
        f"  SHA256: {_sha256(data)}\n"
        f"  MIME: {mime}\n"
        f"  Block type: {block_type}\n"
        f"  Block ID: {_new_block_id(resp)}\n"
        f"  Parent: {parent_page_id}\n"
        f"  Position: {position}\n"
        f"  Upload ID: {upload_id}"
    )


@mcp.tool()
def notion_replace_file(
    block_id: str,
    source: str,
    filename: str = "",
    caption: str = "",
) -> str:
    """Replace the file inside an EXISTING file/image/pdf/video/audio
    block, in place — the block keeps its position on the page. Use this
    to update a document without re-arranging anything; use
    notion_upload_file(position=...) to add a new block instead.

    block_id: the file block to update (from notion_list_files_on_page or
    notion_read_page(include_block_ids=True)).
    source: same schemes as notion_upload_file (`local:`, `shared:`,
    `drive:`, `base64:`).
    caption: when non-empty, replaces the caption; when empty, the
    existing caption is left untouched.

    The new file must map to the same block type as the existing block
    (e.g. you cannot replace an image block's content with a PDF — Notion
    blocks are typed). In that case delete the block and upload anew.
    """
    data, name, mime = _read_source(source, filename)
    block = notion.retrieve_block(block_id)
    btype = block.get("type")
    if btype not in ("file", "image", "pdf", "video", "audio"):
        raise ValueError(
            f"Block {block_id} is type '{btype}', not a file block."
        )
    expected = pick_block_type(mime)
    if expected != btype:
        raise ValueError(
            f"Cannot replace: block {block_id} is a '{btype}' block but the "
            f"new content ({mime}) maps to '{expected}'. Delete the block "
            f"with notion_delete_block and re-upload with "
            f"notion_upload_file(position='<previous-neighbor-block-id>')."
        )
    upload_id = _upload_bytes_to_notion(data, name, mime)
    notion.update_file_block(
        block_id, btype, upload_id,
        caption=caption if caption else None,
        name=name,
    )
    return (
        f"Replaced file in block {block_id} (in place):\n"
        f"  Filename: {name}\n"
        f"  Size: {len(data)} bytes\n"
        f"  SHA256: {_sha256(data)}\n"
        f"  MIME: {mime}\n"
        f"  Block type: {btype}\n"
        f"  Upload ID: {upload_id}"
    )


@mcp.tool()
def notion_import_file_from_url(
    url: str,
    parent_page_id: str,
    filename: str,
    caption: str = "",
    position: str = "end",
    wait_seconds: int = 60,
) -> str:
    """Import a file into Notion directly from a public HTTPS URL — the
    bytes go Notion-side, never through this server or MCP parameters.

    url must be publicly accessible over HTTPS. filename must carry an
    extension Notion accepts (it determines the block type). The import is
    asynchronous: this tool polls until the upload reaches a terminal
    status or wait_seconds elapses. parent_page_id and position behave as
    in notion_upload_file.

    Size limits are plan-dependent (5 MiB free / 5 GiB paid). No sha256 is
    reported — the bytes never pass through this server; verify via
    notion_download_file if integrity matters.
    """
    import time as _time

    upload = notion.create_file_upload(
        filename, mode="external_url", external_url=url,
    )
    upload_id = upload.get("id")
    if not upload_id:
        raise RuntimeError(f"Notion create_file_upload returned no id: {upload}")
    status = upload.get("status", "pending")
    waited = 0.0
    delay = 2.0
    while status == "pending" and waited < max(wait_seconds, 0):
        _time.sleep(min(delay, wait_seconds - waited) or 1)
        waited += delay
        delay = min(delay * 1.5, 15.0)
        status = notion.retrieve_file_upload(upload_id).get("status", "pending")

    if status == "failed":
        detail = notion.retrieve_file_upload(upload_id)
        raise RuntimeError(
            f"Notion failed to import {url}: "
            f"{detail.get('file_import_result', detail)}"
        )
    if status != "uploaded":
        return (
            f"Import still pending after {wait_seconds}s — upload_id "
            f"{upload_id}. Notion imports the URL asynchronously; retry "
            f"this tool with a longer wait_seconds, or smaller file."
        )

    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    block_type = pick_block_type(mime)
    resp = notion.attach_file_to_page(
        parent_page_id, upload_id, filename, caption=caption,
        block_type=block_type, position=_position_param(position),
    )
    return (
        f"Imported from URL to Notion:\n"
        f"  Source URL: {url}\n"
        f"  Filename: {filename}\n"
        f"  Block type: {block_type}\n"
        f"  Block ID: {_new_block_id(resp)}\n"
        f"  Parent: {parent_page_id}\n"
        f"  Position: {position}\n"
        f"  Upload ID: {upload_id}"
    )


@mcp.tool()
def notion_set_page_visual(
    page_id: str,
    target: str,
    source: str = "",
    emoji: str = "",
    filename: str = "",
    remove: bool = False,
) -> str:
    """Set or remove a Notion page's icon or cover image.

    target: 'icon' or 'cover'.
    Provide exactly one of:
      • emoji  — a single emoji character (icon only), e.g. '📊'.
      • source — an image via the usual schemes (`local:`, `shared:`,
        `drive:`, `base64:`); uploaded and attached as the icon/cover.
      • remove=True — clear the icon/cover entirely.
    """
    tgt = target.strip().lower()
    if tgt not in ("icon", "cover"):
        raise ValueError(f"target must be 'icon' or 'cover', got {target!r}.")
    if remove:
        if source or emoji:
            raise ValueError("remove=True takes neither source nor emoji.")
        notion._request("PATCH", f"/pages/{page_id}", json={tgt: None})
        return f"Removed {tgt} from page {page_id}."
    if bool(source) == bool(emoji):
        raise ValueError("Provide exactly one of source or emoji.")
    if emoji:
        if tgt != "icon":
            raise ValueError("emoji is only valid for target='icon'.")
        notion._request(
            "PATCH", f"/pages/{page_id}",
            json={"icon": {"type": "emoji", "emoji": emoji}},
        )
        return f"Set icon of page {page_id} to {emoji}."

    data, name, mime = _read_source(source, filename)
    if not mime.startswith("image/"):
        raise ValueError(
            f"Page {tgt} must be an image; {name!r} is {mime}."
        )
    upload_id = _upload_bytes_to_notion(data, name, mime)
    notion._request(
        "PATCH", f"/pages/{page_id}",
        json={tgt: {"type": "file_upload", "file_upload": {"id": upload_id}}},
    )
    return (
        f"Set {tgt} of page {page_id}:\n"
        f"  Filename: {name}\n"
        f"  Size: {len(data)} bytes\n"
        f"  SHA256: {_sha256(data)}\n"
        f"  Upload ID: {upload_id}"
    )


# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------


@mcp.tool()
def notion_describe_database(database_id: str) -> str:
    """Retrieve a Notion database's schema. Returns each property name and
    type — use this to find the exact files & media column name to pass
    to notion_upload_file_to_database / notion_add_file_to_row.

    database_id can be found by calling notion_list_files_on_page on the
    page that holds the inline database (child_database blocks will show),
    or pasted from the database URL.
    """
    db = notion.get_database(database_id)
    title = "".join(t.get("plain_text", "") for t in db.get("title", [])) \
        or "(untitled)"
    ds_id = notion.resolve_data_source_id(database_id)
    ds = notion.get_data_source(ds_id)
    lines = [
        f"Database: {title}",
        f"  id: {database_id}",
        f"  data_source_id: {ds_id}",
        f"  url: {db.get('url', '')}",
        "",
        "Properties:",
    ]
    for name, p in ds.get("properties", {}).items():
        lines.append(f"  {name!r}  [{p['type']}]")
    return "\n".join(lines)


@mcp.tool()
def notion_upload_file_to_database(
    database_id: str,
    source: str,
    files_property: str,
    title: str,
    title_property: str = "",
    filename: str = "",
) -> str:
    """Upload a file and create a NEW row in a Notion database with the
    file attached to the given files & media column.

    source:         `local:<path>`, `shared:<filename>`,
                    `drive:<name-or-id>`, or `base64:<bytes>` — see
                    notion_upload_file for scheme details and the
                    cross-MCP handoff pattern.
    files_property: exact name of the files & media column
                    (e.g. `"Files & media"`). Use notion_describe_database
                    to confirm the name — it is case-sensitive.
    title:          text value for the row's title column.
    title_property: override the auto-detected title column name.
    filename:       override the source filename.
    """
    data, name, mime = _read_source(source, filename)
    upload_id = _upload_bytes_to_notion(data, name, mime)

    if not title_property:
        ds_id = notion.resolve_data_source_id(database_id)
        ds = notion.get_data_source(ds_id)
        title_property = notion.find_title_property(ds)

    props = {
        title_property: {"title": [{"text": {"content": title}}]},
        files_property: {
            "files": [{
                "type": "file_upload",
                "file_upload": {"id": upload_id},
                "name": name,
            }],
        },
    }
    row = notion.create_database_row(database_id, props)
    return (
        f"Created database row:\n"
        f"  Row ID: {row['id']}\n"
        f"  URL: {row.get('url', '')}\n"
        f"  Title ({title_property}): {title}\n"
        f"  File ({files_property}): {name} ({len(data)} bytes, {mime})\n"
        f"  SHA256: {_sha256(data)}"
    )


@mcp.tool()
def notion_add_file_to_row(
    page_id: str,
    source: str,
    files_property: str,
    mode: str = "append",
    filename: str = "",
) -> str:
    """Upload a file and attach it to an EXISTING database row's files &
    media column.

    page_id:        the row (page) ID — paste from the row's "Copy link"
                    or obtain via notion_search_pages.
    source:         same schemes as notion_upload_file: `local:<path>`,
                    `shared:<filename>`, `drive:<name-or-id>`, or
                    `base64:<bytes>`.
    files_property: exact name of the files & media column.
    mode:           `append` (default) keeps existing files and adds the
                    new one. `replace` overwrites the column with just the
                    new file.
    filename:       override the source filename.

    ## Cross-MCP file transfer
    To attach a Gmail attachment to an existing Notion row:
      1. google-accounts-mcp download_attachment(msg_id, name, destination='shared')
      2. notion_add_file_to_row(page_id, source='shared:<name>',
                                files_property='<column>')
    Files never traverse MCP parameters — no size limits. Alternative via
    Drive: download_attachment → drive_upload(local_filename=...) →
    notion_add_file_to_row(source='drive:<file_id>', ...).
    """
    result = _add_file_to_row(page_id, source, files_property, mode, filename)
    return (
        f"{'Appended' if result['mode'] == 'append' else 'Replaced'} file on row:\n"
        f"  Page ID: {page_id}\n"
        f"  Column: {files_property}\n"
        f"  File: {result['name']} ({result['size']} bytes, {result['mime']})\n"
        f"  SHA256: {result['sha256']}\n"
        f"  Total files in column: {result['total_files']}"
    )


# Batch file-adds run with bounded concurrency: Notion's file-upload API
# isn't pipelined and each row costs ~7-58s serially, so a serial batch of ~8
# blows past the MCP client's 240s ceiling. Uploading a few rows in parallel
# (each tool call already runs in the threadpool; notion_client uses a fresh,
# thread-safe httpx.Client per request; the 429/5xx backoff covers bursts)
# keeps wall-time ≈ ceil(N / concurrency) × per-item, well under the ceiling.
# Tunable via NOTION_BATCH_CONCURRENCY. See feedback_notion_batch_attach_timeout.
_BATCH_CONCURRENCY = max(1, int(os.environ.get("NOTION_BATCH_CONCURRENCY", "3")))

# 'append' is a read-modify-write of a row's files property, so two concurrent
# items targeting the SAME page would race and lose a file. A per-page lock
# serialises same-page work while different pages still run in parallel.
_page_locks: dict[str, threading.Lock] = {}
_page_locks_guard = threading.Lock()


def _page_lock(page_id: str) -> threading.Lock:
    with _page_locks_guard:
        lk = _page_locks.get(page_id)
        if lk is None:
            lk = threading.Lock()
            _page_locks[page_id] = lk
        return lk


def _add_file_to_row(
    page_id: str,
    source: str,
    files_property: str,
    mode: str = "append",
    filename: str = "",
) -> dict:
    """Core of notion_add_file_to_row. Returns a dict with the fields
    both the single-shot and batch tools need to format their output."""
    mode_norm = mode.strip().lower()
    if mode_norm not in ("append", "replace"):
        raise ValueError(f"mode must be 'append' or 'replace', got {mode!r}.")

    data, name, mime = _read_source(source, filename)
    upload_id = _upload_bytes_to_notion(data, name, mime)

    new_entry = {
        "type": "file_upload",
        "file_upload": {"id": upload_id},
        "name": name,
    }

    if mode_norm == "append":
        page = notion.retrieve_page(page_id)
        prop = page.get("properties", {}).get(files_property)
        if prop is None:
            raise ValueError(
                f"Property {files_property!r} not found on page {page_id}."
            )
        if prop.get("type") != "files":
            raise ValueError(
                f"Property {files_property!r} is type '{prop.get('type')}', "
                f"not 'files'."
            )
        files_list = list(prop.get("files", [])) + [new_entry]
    else:
        files_list = [new_entry]

    notion.update_page_properties(
        page_id, {files_property: {"files": files_list}}
    )
    return {
        "page_id": page_id,
        "files_property": files_property,
        "mode": mode_norm,
        "name": name,
        "size": len(data),
        "sha256": _sha256(data),
        "mime": mime,
        "total_files": len(files_list),
    }


@mcp.tool()
def notion_batch_add_file_to_row(
    items_json: str,
    default_files_property: str = "",
    default_mode: str = "append",
) -> str:
    """Attach files to many existing Notion rows in one call.

    items_json is a JSON array of objects. Each item takes the same
    shape as the single-shot tool's parameters::

        [
          {"page_id": "...", "source": "shared:invoice1.pdf"},
          {"page_id": "...", "source": "shared:invoice2.pdf",
           "filename": "Renamed.pdf"},
          {"page_id": "...", "source": "drive:<id>",
           "files_property": "Attachments", "mode": "replace"}
        ]

    default_files_property and default_mode fill in any item that
    omits those fields — pass them once at the batch level instead of
    repeating per item.

    Items are uploaded with bounded concurrency (NOTION_BATCH_CONCURRENCY,
    default 3) so a batch of N finishes in roughly ceil(N/concurrency) waves
    rather than serially — a batch of ~8 used to exceed the client's 4-minute
    timeout when serial. Concurrent items targeting the SAME row are still
    serialised (the 'append' path is a read-modify-write). The 429/5xx backoff
    in the client absorbs any rate-limit bursts. Per-item failures are reported
    in the output (in input order) but do NOT abort the batch — use the
    per-item status to retry individual items.

    Files are staged in the shared mount; the typical pattern is to stage N
    files via google-accounts-mcp download_attachment, call this tool once, then
    call notion_purge_file to remove each staged file."""
    try:
        items = json.loads(items_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"items_json is not valid JSON: {e}") from e
    if not isinstance(items, list) or not items:
        raise ValueError("items_json must be a non-empty JSON array.")

    # Validate/normalise up front; keep input order via index.
    results: dict[int, tuple[bool, str]] = {}
    runnable: list[tuple[int, str, str, str, str, str]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            results[i] = (False, f"  [{i}] SKIP: not a JSON object: {item!r}")
            continue
        page_id = item.get("page_id")
        source = item.get("source")
        files_property = item.get("files_property") or default_files_property
        mode = item.get("mode") or default_mode
        filename = item.get("filename", "")
        if not page_id or not source or not files_property:
            results[i] = (
                False,
                f"  [{i}] SKIP: missing page_id/source/files_property on {item!r}",
            )
            continue
        runnable.append((i, page_id, source, files_property, mode, filename))

    def _run(i, page_id, source, files_property, mode, filename):
        try:
            # Serialise same-page work to protect the append read-modify-write.
            with _page_lock(page_id):
                r = _add_file_to_row(page_id, source, files_property, mode, filename)
            return i, (
                True,
                f"  [{i}] OK page={page_id} file={r['name']} "
                f"({r['size']} bytes, {r['mime']}) total_files={r['total_files']}",
            )
        except Exception as e:  # noqa: BLE001 - isolate per-item failures
            return i, (False, f"  [{i}] FAIL page={page_id} source={source}: {e}")

    workers = max(1, min(_BATCH_CONCURRENCY, len(runnable)))
    if runnable:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for fut in concurrent.futures.as_completed(
                ex.submit(_run, *args) for args in runnable
            ):
                i, outcome = fut.result()
                results[i] = outcome

    ok = sum(1 for v in results.values() if v[0])
    failed = len(results) - ok
    header = f"Batch add file to row — {len(items)} item(s), concurrency={workers}:\n"
    # Soft-warn: at ~15s/item worst case, ceil(N/conc)*15 should stay under the
    # client's 240s ceiling; flag batches large enough to risk it.
    warn = ""
    if len(items) > workers * 12:
        warn = (
            f"\n⚠ Large batch ({len(items)} items at concurrency {workers}) may "
            f"approach the client's 4-min timeout — consider splitting if it stalls."
        )
    body = "\n".join(results[i][1] for i in sorted(results))
    return header + body + f"\n\nSummary: {ok} ok, {failed} failed" + warn


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


@mcp.tool()
def notion_download_file(
    block_id: str,
    destination: str = "local",
    filename: str = "",
) -> str:
    """Download a file from a Notion file/image/pdf/video/audio block.

    destination is one of: `local`, `shared`, `drive`, `base64`.
      • `local`  — the notion-mcp private store (FILES_DIR).
      • `shared` — the cross-MCP shared mount, immediately attachable by
        google-accounts-mcp as attachments=['shared:<filename>'] on
        draft_email / send_email. 24h TTL applies.
      • `drive`  — upload to the shared Drive folder (DRIVE_FOLDER_NAME).
      • `base64` — inline; subject to MCP response size limits.

    filename overrides the inferred name. When destination=local, filename
    may include subdirectories (e.g. `reports/q3.pdf`); they are created
    under the local store. Use notion_list_files_on_page to find block IDs.

    ## Cross-MCP file transfer (Notion → Gmail)
    To email a file stored in Notion:
      1. notion_download_file(block_id, destination='shared')
      2. google-accounts-mcp draft_email(..., attachments=['shared:<filename>'])
    Bytes never traverse MCP parameters — no size limits.
    """
    name, mime, content = notion.fetch_file_block_bytes(block_id)
    target_name = filename or name
    dest = destination.strip().lower()

    if dest == "local":
        path = _safe_local_path(target_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        rel = path.relative_to(Path(FILES_DIR).resolve())
        return (
            f"Downloaded from Notion:\n"
            f"  Filename: {target_name}\n"
            f"  Size: {len(content)} bytes\n"
            f"  SHA256: {_sha256(content)}\n"
            f"  MIME: {mime}\n"
            f"  Saved to: /data/files/{rel}\n"
            f"  Reference as source: local:{rel}"
        )

    if dest == "shared":
        safe_name = os.path.basename(target_name)
        if not safe_name or safe_name != target_name:
            raise ValueError(
                f"Invalid filename {target_name!r} for destination='shared' "
                f"— must be a bare filename, no path components."
            )
        path = _atomic_create(Path(SHARED_DIR), safe_name, content)
        actual_name = path.name
        rename_note = (
            ""
            if actual_name == safe_name
            else f"  Note: '{safe_name}' already existed; saved as '{actual_name}'.\n"
        )
        return (
            f"Downloaded from Notion to shared storage:\n"
            f"  Filename: {actual_name}\n"
            f"  Size: {len(content)} bytes\n"
            f"  SHA256: {_sha256(content)}\n"
            f"  MIME: {mime}\n"
            f"  Saved to: {path}\n"
            f"{rename_note}"
            f"  Reference from google-accounts-mcp: attachments=['shared:{actual_name}']\n"
            f"  TTL: shared-storage files may be purged by a host-side sweep "
            f"(24h in the reference deployment) — attach promptly."
        )

    if dest == "drive":
        file = drive.upload_bytes(target_name, content, mime)
        return (
            f"Downloaded from Notion → uploaded to Drive:\n"
            f"  Filename: {file['name']}\n"
            f"  Size: {len(content)} bytes\n"
            f"  SHA256: {_sha256(content)}\n"
            f"  MIME: {mime}\n"
            f"  Drive file ID: {file['id']}\n"
            f"  Link: {file.get('webViewLink', '(not shared)')}"
        )

    if dest == "base64":
        encoded = base64.standard_b64encode(content).decode("ascii")
        return (
            f"Notion file:\n"
            f"  Filename: {target_name}\n"
            f"  Size: {len(content)} bytes\n"
            f"  SHA256: {_sha256(content)}\n"
            f"  MIME: {mime}\n"
            f"  Base64:\n{encoded}"
        )

    raise ValueError(
        f"Unknown destination '{destination}'. "
        f"Use 'local', 'shared', 'drive', or 'base64'."
    )


# ---------------------------------------------------------------------------
# PDF reading (text extraction + OCR + page render) — semantics shared with
# google-accounts-mcp via the duplicated pdf_read module.
# ---------------------------------------------------------------------------

# Caps concurrent rasterisation/parse so a burst of PDF work can't spike RSS
# past the container MemoryMax (pypdfium2 holds large native bitmaps while
# rendering). Tunable via PDF_MAX_CONCURRENCY. Same convention as google-accounts-mcp.
_PDF_CONCURRENCY = threading.Semaphore(
    max(1, int(os.environ.get("PDF_MAX_CONCURRENCY", "4")))
)

# Maximum size of text returned by notion_extract_file_text before it is
# truncated on a page boundary — fits typical MCP response ceilings.
EXTRACT_TEXT_MAX_BYTES = 200_000


def _read_pdf_source(source: str) -> tuple[bytes, str]:
    """Resolve a source scheme to bytes and reject non-PDFs."""
    data, name, mime = _read_source(source)
    is_pdf = (
        mime == "application/pdf"
        or name.lower().endswith(".pdf")
        or data[:5] == b"%PDF-"
    )
    if not is_pdf:
        raise ValueError(
            f"'{name}' is {mime}, not a PDF. Only PDFs are supported; for "
            f"text files download the bytes with notion_download_file."
        )
    return data, name


@mcp.tool()
def notion_extract_file_text(
    source: str,
    pages: str | None = None,
    mode: str = "text",
    ocr: str = "auto",
) -> str:
    """Extract text from a PDF anywhere notion-mcp can reach — a file
    stored in Notion, the shared cross-MCP mount, the private store, or
    the Drive folder — without moving the bytes over MCP.

    source schemes: 'notion:<block_id>' (file/pdf block on a page — get
    block IDs from notion_list_files_on_page), 'shared:<filename>',
    'local:<path>', 'drive:<name-or-id>'.

    Returns JSON: {"text", "page_count", "pages_returned", "mode",
    "truncated", "filename"} (+ "ocr_used"/"ocr_pages"/"ocr_engine" when
    OCR ran). Response ceiling 200 KB — truncation happens on a page
    boundary with a trailing marker naming the pages to fetch next.

    pages: pdftotext-style spec — None/"" (all), "3", "1-5", "1,3,5",
      "1-3,7". Out-of-range pages are silently dropped.
    mode: 'text' (flowing), 'layout' (preserve columns), 'tables'
      (extract_tables → markdown). Applies to native extraction only.

    OCR (scanned / image-only PDFs) — delegated to the fleet xberg
    service (XBERG_BASE_URL env; pages rasterised locally, sent as PNGs):
      ocr='auto' (default) — pages whose native text is < 20 chars are
        OCR'd (tesseract backend, seconds/page). "ocr_error" in the
        response means OCR was needed but xberg was unreachable/failed.
      ocr='off'   — native extraction only.
      ocr='force' — OCR every requested page (garbled font encodings).
      ocr='llm'   — transcribe pages with a vision model (xberg vlm
        backend → LiteLLM; env: XBERG_VLM_API_KEY, optional
        XBERG_VLM_MODEL / XBERG_VLM_BASE_URL). Best for handwriting and
        messy tables; slower and metered."""
    pdf_read.validate_options(mode, ocr)
    data, name = _read_pdf_source(source)
    result = pdf_read.extract_pdf_text(
        data,
        pages=pages,
        mode=mode,
        ocr=ocr,
        max_bytes=EXTRACT_TEXT_MAX_BYTES,
        concurrency=_PDF_CONCURRENCY,
    )
    result["filename"] = name
    result["source"] = source
    return json.dumps(result, indent=2)


@mcp.tool()
def notion_render_file_page(
    source: str,
    page: int = 1,
    max_width: int = 1200,
    format: str = "jpeg",
    quality: int = 75,
    return_base64: bool = False,
):
    # No return annotation on purpose: the tool returns either an MCP Image
    # (FastMCP converts it to an image content block at the protocol layer)
    # or a JSON string, and FastMCP 1.x tries — and fails — to build a
    # pydantic structured-output schema for any annotated non-str union
    # containing Image.
    """Render one page of a PDF (from Notion, shared, local, or Drive) as
    a viewable image. The default return is a real MCP image content
    block, so you SEE the page directly — for a scanned document this is
    often all you need. Use when notion_extract_file_text isn't enough:
    charts, stamps, layouts, visual validation.

    source schemes as in notion_extract_file_text ('notion:<block_id>',
    'shared:<filename>', 'local:<path>', 'drive:<name-or-id>').

    Set return_base64=True for a JSON envelope {"image_base64",
    "mime_type", "width", "height", "page", "page_count", "filename"}
    instead (for relaying bytes programmatically). Errors return a JSON
    string with an "error" key in both modes.

    page: 1-indexed. max_width: 200..4000 px (default 1200).
    format: 'jpeg' (default) or 'png'. quality: JPEG 1..100 (default 75)."""
    fmt = (format or "jpeg").strip().lower()
    if fmt not in ("png", "jpeg", "jpg"):
        raise ValueError(f"format must be 'png' or 'jpeg' — got {format!r}.")
    if fmt == "jpg":
        fmt = "jpeg"
    if not (1 <= quality <= 100):
        raise ValueError(f"quality must be in 1..100 — got {quality}.")
    if not (200 <= max_width <= 4000):
        raise ValueError(f"max_width must be in 200..4000 — got {max_width}.")
    if not isinstance(page, int) or page < 1:
        raise ValueError(f"page must be a 1-indexed integer — got {page!r}.")

    data, name = _read_pdf_source(source)
    try:
        img_bytes, out_mime, out_w, out_h, page_count = (
            pdf_read.render_pdf_page_bytes(
                data, page,
                max_width=max_width,
                fmt=fmt,
                quality=quality,
                concurrency=_PDF_CONCURRENCY,
            )
        )
    except pdf_read.PageOutOfRangeError as e:
        return json.dumps(
            {"error": str(e), "page_count": e.page_count}, indent=2,
        )

    if return_base64:
        return json.dumps(
            {
                "image_base64": base64.standard_b64encode(img_bytes).decode(
                    "ascii"
                ),
                "mime_type": out_mime,
                "width": out_w,
                "height": out_h,
                "page": page,
                "page_count": page_count,
                "filename": name,
            },
            indent=2,
        )
    return Image(
        data=img_bytes,
        format="jpeg" if out_mime == "image/jpeg" else "png",
    )


# ---------------------------------------------------------------------------
# Page read / write
# ---------------------------------------------------------------------------


@mcp.tool()
def notion_read_page(
    page_id: str, max_depth: int = 3, include_block_ids: bool = False,
) -> str:
    """Read a Notion page's properties and content as markdown.

    Returns the page title, properties, and full block content rendered as
    readable markdown. Nested blocks (toggles, list children, columns) are
    fetched recursively up to max_depth levels.

    page_id: Notion page ID (from notion_search_pages or a URL).
    max_depth: how many levels of nested blocks to fetch (1-5, default 3).
    include_block_ids: if True, each block line is suffixed with an HTML
        comment containing the block UUID, e.g. <!-- block:abc123 -->.
        Use these IDs with notion_update_block / notion_delete_block.
    """
    max_depth = max(1, min(5, max_depth))
    page = notion.retrieve_page(page_id)
    title = notion.get_page_title(page)
    props = format_page_properties(page.get("properties", {}))

    blocks = notion.get_block_children_deep(page_id, max_depth=max_depth)
    content = blocks_to_markdown(blocks, include_ids=include_block_ids)

    parts = [f"# {title}", f"  id: {page_id}", f"  url: {page.get('url', '')}"]
    if props:
        parts.append(f"\nProperties:\n{props}")
    if content:
        parts.append(f"\nContent:\n{content}")
    elif not blocks:
        parts.append("\n(empty page)")
    return "\n".join(parts)


@mcp.tool()
def notion_create_page(
    title: str,
    content: str = "",
    parent_page_id: str = "",
    database_id: str = "",
    properties_json: str = "",
    icon: str = "",
    template: str = "",
    position: str = "",
    silent: bool = False,
    native_markdown: bool = False,
    markdown_version: str = "",
) -> str:
    """Create a new Notion page with optional markdown content.

    For a standalone page under another page, provide parent_page_id.
    For a database row, provide database_id and properties_json.

    title:          the page title.
    content:        markdown text that becomes the page body. Supports
                    headings, lists, quotes, code blocks, inline formatting.
    parent_page_id: ID of the parent page.
    database_id:    ID of a database to add the row to.
    properties_json: JSON object of extra Notion properties (for database
                    rows). Keys are property names, values are Notion API
                    property value objects. A wiki page's verification is
                    set here too: {"Verification": {"verification":
                    {"state": "verified"}}}.
    icon:           an emoji ("🚀"), "custom_emoji:<name-or-id>" (see
                    notion_list_custom_emojis), "icon:<name>[:<color>]" for a
                    native Notion icon (e.g. "icon:pizza:blue"), or an image URL.
    template:       for database rows: "default" applies the database's
                    default template, or pass a template ID from
                    notion_list_templates. The template supplies the body,
                    so leave content empty.
    position:       where the page lands among a parent PAGE's children:
                    "start", "end", or a block ID to insert after.
    silent:         True skips the update, @mention and Person-property
                    notifications this write would send.
    native_markdown: True sends content to Notion's own markdown parser
                    instead of converting it to blocks here. Use it for
                    Notion-specific markup (tables, callouts, columns,
                    toggles, mentions) and for very large pages, which
                    Notion then processes as a background task.
    markdown_version: with native_markdown, "v2" opts into Notion-flavored
                    Markdown (a preview); default is the stable syntax.
    """
    properties = json.loads(properties_json) if properties_json else {}

    # Set title — find the title property name or default to "title"
    if database_id and not any(
        "title" in (v.get("type", "") if isinstance(v, dict) else "")
        for v in properties.values()
    ):
        ds_id = notion.resolve_data_source_id(database_id)
        ds = notion.get_data_source(ds_id)
        title_prop = notion.find_title_property(ds)
        properties[title_prop] = {"title": [{"text": {"content": title}}]}
    elif not database_id:
        properties["title"] = [{"text": {"content": title}}]

    template_obj = None
    if template:
        if content:
            return "A template supplies the page body — leave content empty."
        template_obj = (
            {"type": "default"} if template.lower() == "default"
            else {"type": "template_id", "template_id": template}
        )

    position_obj = None
    if position.strip().lower() in ("start", "end"):
        position_obj = {"type": f"page_{position.strip().lower()}"}
    elif position.strip():
        position_obj = {
            "type": "after_block", "after_block": {"id": position.strip()},
        }

    use_native = native_markdown and bool(content)
    page = notion.create_page(
        properties=properties,
        parent_page_id=parent_page_id,
        database_id=database_id,
        children=markdown_to_blocks(content) if content and not use_native else None,
        icon=_icon_param(icon) if icon else "",
        markdown=content if use_native else "",
        markdown_version=markdown_version if use_native else "",
        template=template_obj,
        position=position_obj,
        silent=silent,
    )
    pending = _pending_task_note(page)
    if pending:
        return pending
    return (
        f"Created page:\n"
        f"  Title: {title}\n"
        f"  ID: {page['id']}\n"
        f"  URL: {page.get('url', '')}"
    )


@mcp.tool()
def notion_update_page(
    page_id: str,
    title: str = "",
    properties_json: str = "",
    icon: str = "",
    silent: bool = False,
) -> str:
    """Update a Notion page's title, icon, or other properties.

    page_id:         the page to update.
    title:           new title (leave empty to keep current).
    properties_json: JSON object of properties to update. Keys are property
                     names, values are Notion API property value objects.
                     Omitted properties are left unchanged. Wiki
                     verification is a property too: {"Verification":
                     {"verification": {"state": "verified"}}}.
    icon:            "remove" to clear it, or an emoji,
                     "custom_emoji:<name-or-id>", "icon:<name>[:<color>]"
                     (native Notion icon) or an image URL.
    silent:          True skips the update, @mention and Person-property
                     notifications this write would send.
    """
    props: dict = {}
    if properties_json:
        props = json.loads(properties_json)
    if title:
        # Check if this is a database page (has a title property name)
        try:
            page = notion.retrieve_page(page_id)
            for name, p in page.get("properties", {}).items():
                if p.get("type") == "title":
                    props[name] = {"title": [{"text": {"content": title}}]}
                    break
        except Exception:
            props["title"] = [{"text": {"content": title}}]

    body: dict = {}
    if props:
        body["properties"] = props
    if icon:
        body["icon"] = _icon_param(icon)

    if not body:
        return "Nothing to update — provide title, properties_json, or icon."

    notion.update_page(page_id, body, silent=silent)
    return f"Updated page {page_id}."


@mcp.tool()
def notion_append_content(
    page_id: str, content: str, position: str = "end", silent: bool = False,
) -> str:
    """Add markdown content to an existing Notion page.

    Parses the markdown into blocks and adds them to the page.
    Supports: headings (# ## ###), bullet/numbered/to-do lists, quotes,
    fenced code blocks, dividers (---), paragraphs with inline formatting
    (**bold**, *italic*, `code`, ~~strike~~, [links](url)).

    page_id:  the page (or any block that supports children) to add to.
    content:  markdown text to add.
    position: 'end' (default), 'start', or a block ID to insert directly
              AFTER that block (IDs from
              notion_read_page(include_block_ids=True)).
    silent:   True skips the notifications this write would send.
    """
    if not content.strip():
        return "No content to append."
    blocks = markdown_to_blocks(content)
    added = notion.append_blocks(
        page_id, blocks, position=_position_param(position), silent=silent,
    )
    return f"Appended {len(added)} block(s) to page {page_id}."


@mcp.tool()
def notion_add_tabs(
    page_id: str, tabs_json: str, position: str = "end",
) -> str:
    """Add a tab block (a row of tabs, each with its own content) to a page.

    page_id:   the page (or any block that supports children) to add to.
    tabs_json: JSON array, one object per tab:
               [{"label": "Overview", "icon": "📋", "content": "markdown…"},
                {"label": "Details", "content": "…"}]
               label is required; icon (an emoji) and content are optional.
    position:  'end' (default), 'start', or a block ID to insert after.
    """
    tabs = json.loads(tabs_json)
    if not isinstance(tabs, list) or not tabs or not all(
        isinstance(t, dict) and t.get("label") for t in tabs
    ):
        return "tabs_json must be a non-empty JSON array of {label, icon?, content?}."
    labels = []
    for t in tabs:
        para: dict = {"rich_text": md_to_rich_text(t["label"])}
        if t.get("icon"):
            para["icon"] = {"emoji": t["icon"]}
        labels.append({"type": "paragraph", "paragraph": para})
    created = notion.append_blocks(
        page_id, [{"type": "tab", "tab": {"children": labels}}],
        position=_position_param(position),
    )
    tab_id = created[0]["id"]
    # Content goes in a second pass: a single request cannot nest deeply
    # enough for tab → label → content that itself has children.
    tab_items = notion.list_children(tab_id)
    for t, item in zip(tabs, tab_items):
        if (t.get("content") or "").strip():
            notion.append_blocks(item["id"], markdown_to_blocks(t["content"]))
    return (
        f"Added a tab block with {len(tabs)} tab(s) to {page_id}.\n"
        f"  Block ID: {tab_id}"
    )


@mcp.tool()
def notion_archive_page(page_id: str) -> str:
    """Archive (soft-delete) a Notion page. The page can be restored from
    the Notion trash within 30 days using notion_restore_page.

    page_id: the page to archive.
    """
    notion.archive_page(page_id)
    return f"Archived page {page_id}."


@mcp.tool()
def notion_restore_page(page_id: str) -> str:
    """Restore an archived page from the Notion trash.

    page_id: the archived page to restore.
    """
    notion.restore_page(page_id)
    return f"Restored page {page_id} from trash."


# ---------------------------------------------------------------------------
# Block operations
# ---------------------------------------------------------------------------


@mcp.tool()
def notion_update_block(block_id: str, content: str, block_type: str = "") -> str:
    """Update an existing block's text content.

    block_id:   ID of the block to update (from notion_read_page output).
    content:    new text for the block (supports inline markdown).
    block_type: the block type (paragraph, heading_1, heading_2, heading_3,
                bulleted_list_item, numbered_list_item, to_do, quote, callout,
                toggle, code). If omitted, the block is fetched to detect it.
    """
    if not block_type:
        block = notion._request("GET", f"/blocks/{block_id}")
        block_type = block.get("type", "paragraph")

    rich_text = md_to_rich_text(content)
    update: dict = {block_type: {"rich_text": rich_text}}
    notion.update_block(block_id, update)
    return f"Updated {block_type} block {block_id}."


@mcp.tool()
def notion_delete_block(block_id: str) -> str:
    """Delete a block from a Notion page. This removes the block and all its
    children. The deletion is permanent (blocks cannot be restored).

    block_id: ID of the block to delete.
    """
    notion.delete_block(block_id)
    return f"Deleted block {block_id}."


# ---------------------------------------------------------------------------
# Database query / create
# ---------------------------------------------------------------------------


@mcp.tool()
def notion_query_database(
    database_id: str,
    filter_json: str = "",
    sorts_json: str = "",
    limit: int = 50,
    start_cursor: str = "",
    properties: str = "",
    archived: bool = False,
) -> str:
    """Query a Notion database with optional filter, sort, and pagination.

    Returns matching rows with their properties. Use notion_describe_database
    first to see the schema and property names.

    database_id: the database to query.
    filter_json: Notion API filter object as JSON. Example:
        {"property": "Status", "select": {"equals": "Done"}}
      Compound example:
        {"and": [
          {"property": "Status", "select": {"equals": "Active"}},
          {"property": "Priority", "number": {"greater_than": 3}}
        ]}
      Status properties can also be filtered by group (To-do / In
      progress / Complete): {"property": "Status",
        "status": {"group_equals": "In progress"}}
    sorts_json:  JSON array of sort objects. Example:
        [{"property": "Due", "direction": "ascending"}]
      Use "ascending" or "descending".
    limit:       page size (default 50, max 100 — Notion's hard cap).
    start_cursor: opaque cursor returned by a previous call. Pass it
      to fetch the next page. When the output shows
      ``next_cursor: <token>`` the result set has more rows; when it
      shows ``next_cursor: (end)`` you have reached the end.
    properties: optional comma-separated list of property names to
      include (e.g. ``"Gmail Message ID, Invoice, Status"``). Slims
      the output dramatically when you only need a few columns. The
      title column is always shown. Unknown names are ignored silently
      so a caller can ask for a union across a few databases.
    archived: True returns the database's archived rows instead of the
      active ones.
    """
    limit = max(1, min(100, limit))
    filter_obj = json.loads(filter_json) if filter_json else None
    sorts_obj = json.loads(sorts_json) if sorts_json else None

    wanted: set[str] | None = None
    if properties.strip():
        wanted = {p.strip() for p in properties.split(",") if p.strip()}

    ds_id = notion.resolve_data_source_id(database_id)
    envelope = notion.query_data_source_page(
        ds_id,
        filter=filter_obj,
        sorts=sorts_obj,
        page_size=limit,
        start_cursor=start_cursor,
        is_archived=archived,
    )
    rows = envelope.get("results", [])
    next_cursor = envelope.get("next_cursor") or ""
    has_more = bool(envelope.get("has_more"))

    if not rows:
        suffix = f" (start_cursor={start_cursor!r})" if start_cursor else ""
        return f"No results in database {database_id}{suffix}."

    lines = [f"{len(rows)} row(s) from database {database_id}:\n"]
    for row in rows:
        title = notion.get_page_title(row)
        lines.append(f"--- {title} ---")
        lines.append(f"  id: {row['id']}")
        lines.append(f"  url: {row.get('url', '')}")
        for name, prop in row.get("properties", {}).items():
            if prop.get("type") == "title":
                continue  # Already shown
            if wanted is not None and name not in wanted:
                continue
            val = format_property_value(prop)
            if val:
                lines.append(f"  {name}: {val}")
        lines.append("")

    lines.append(
        f"has_more: {has_more}  next_cursor: {next_cursor or '(end)'}"
    )
    if has_more:
        lines.append(
            "  Pass next_cursor back as start_cursor to fetch the next page."
        )
    if (envelope.get("request_status") or {}).get("type") == "incomplete":
        lines.append(
            "WARNING: Notion stopped this query at its 10,000-result limit — "
            "rows beyond it are NOT included. Narrow the filter to see them."
        )
    return "\n".join(lines)


@mcp.tool()
def notion_create_database(
    parent_page_id: str,
    title: str = "",
    schema_json: str = "",
    database_type: str = "",
    inline: bool = False,
) -> str:
    """Create a new Notion database under a page.

    parent_page_id: page to create the database under.
    title:          database title (optional for a typed database, which
                    is otherwise named after its type).
    schema_json:    JSON object mapping property names to their type config
                    in Notion API format. A title property is required.
                    Example:
                    {
                      "Name": {"title": {}},
                      "Status": {"select": {"options": [
                        {"name": "To Do", "color": "red"},
                        {"name": "Done", "color": "green"}
                      ]}},
                      "Due Date": {"date": {}},
                      "Priority": {"number": {"format": "number"}},
                      "Tags": {"multi_select": {"options": [
                        {"name": "bug"}, {"name": "feature"}
                      ]}},
                      "Assignee": {"people": {}},
                      "Done": {"checkbox": {}},
                      "Notes": {"rich_text": {}},
                      "Link": {"url": {}}
                    }
    database_type:  "tasks", "projects" or "skills" creates one of Notion's
                    typed databases with its built-in schema INSTEAD of
                    schema_json (they cannot be combined). Run
                    notion_describe_database afterwards for the exact
                    property names.
    inline:         True shows the database inline on the parent page;
                    default is a full-page database.
    """
    if bool(schema_json) == bool(database_type):
        return "Provide exactly one of schema_json or database_type."
    db = notion.create_database(
        parent_page_id, title,
        properties=json.loads(schema_json) if schema_json else None,
        database_type=database_type, is_inline=inline,
    )
    shown = title or "".join(
        t.get("plain_text", "") for t in db.get("title", [])
    )
    return (
        f"Created database:\n"
        f"  Title: {shown}\n"
        f"  ID: {db['id']}\n"
        f"  URL: {db.get('url', '')}"
    )


@mcp.tool()
def notion_add_data_source(
    database_id: str, title: str, schema_json: str,
) -> str:
    """Add another data source (a second table with its own schema) to an
    existing database. Most databases have one; the other database tools
    here operate on a database's FIRST data source.

    database_id: the database to add to.
    title:       name of the new data source.
    schema_json: property schema, same format as notion_create_database
                 (a title property is required).
    """
    ds = notion.create_data_source(database_id, title, json.loads(schema_json))
    return (
        f"Added data source:\n"
        f"  Title: {title}\n"
        f"  Data source ID: {ds['id']}\n"
        f"  Database: {database_id}"
    )


@mcp.tool()
def notion_list_templates(database_id: str, name: str = "") -> str:
    """List the page templates defined on a database. Pass a template's ID
    (or "default") as notion_create_page's template argument to create a
    row from it.

    database_id: the database to inspect.
    name:        optional case-insensitive substring filter.
    """
    ds_id = notion.resolve_data_source_id(database_id)
    templates = notion.list_templates(ds_id, name=name)
    if not templates:
        return f"No templates on database {database_id}."
    lines = [f"{len(templates)} template(s) on database {database_id}:\n"]
    for t in templates:
        default = " (default)" if t.get("is_default") else ""
        lines.append(f"  • {t.get('name', '(unnamed)')}{default}")
        lines.append(f"      id: {t['id']}")
    return "\n".join(lines)


@mcp.tool()
def notion_update_database(
    database_id: str,
    title: str = "",
    description: str = "",
    properties_json: str = "",
) -> str:
    """Update a Notion database's title, description, or property schema.

    database_id:     the database to update.
    title:           new database title (leave empty to keep current).
    description:     new description (leave empty to keep current).
    properties_json: JSON object of property changes. Notion API format:
      - Add property:    {"New Column": {"rich_text": {}}}
      - Remove property: {"Old Column": null}
      - Rename property: {"Old Column": {"name": "New Column"}}
      - Change type:     {"Column": {"number": {"format": "percent"}}}
      - Add select opts: {"Status": {"select": {"options": [
            {"name": "New Option", "color": "blue"}
        ]}}}
      - Recolour an existing select / multi_select / status option: send
        it by name with the new color (names match case-insensitively).

    Multiple changes can be combined in one call. Use
    notion_describe_database first to see current schema.
    """
    props = json.loads(properties_json) if properties_json else None
    db = notion.update_database(
        database_id, title=title, description=description, properties=props,
    )
    new_title = "".join(
        t.get("plain_text", "") for t in db.get("title", [])
    ) or "(untitled)"
    lines = [f"Updated database:", f"  Title: {new_title}", f"  ID: {database_id}", ""]
    lines.append("Current properties:")
    for name, p in db.get("properties", {}).items():
        lines.append(f"  {name} [{p['type']}]")
    return "\n".join(lines)


@mcp.tool()
def notion_get_property(
    page_id: str,
    property_name: str = "",
    property_id: str = "",
) -> str:
    """Retrieve a single property from a Notion page with full pagination.

    Useful for large properties (relations, rollups, long rich_text) that
    get truncated in the normal page response. Provide either property_name
    or property_id.

    page_id:       the page to read from.
    property_name: human-readable property name (looked up from page metadata).
    property_id:   Notion property ID (from notion_describe_database schema).
                   Takes precedence over property_name if both given.
    """
    if not property_id and not property_name:
        return "Provide either property_name or property_id."

    if not property_id:
        page = notion.retrieve_page(page_id)
        for name, prop in page.get("properties", {}).items():
            if name == property_name:
                property_id = prop.get("id", "")
                break
        if not property_id:
            return f"Property '{property_name}' not found on page {page_id}."

    result = notion.retrieve_page_property(page_id, property_id)

    if result.get("object") == "list":
        items = result.get("results", [])
        ptype = result.get("type", "unknown")
        lines = [f"Property ({ptype}), {len(items)} item(s):\n"]
        for item in items:
            lines.append(f"  {format_property_value(item)}")
        return "\n".join(lines)

    return f"Property value:\n  {format_property_value(result)}"


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


@mcp.tool()
def notion_get_comments(page_id: str) -> str:
    """Get all comments on a Notion page.

    Returns comment text, author, and timestamps. Use this to read
    discussion threads on a page.

    page_id: the page to get comments from.
    """
    comments = notion.list_comments(page_id)
    if not comments:
        return f"No comments on page {page_id}."

    lines = [f"{len(comments)} comment(s) on page {page_id}:\n"]
    for c in comments:
        author = c.get("created_by", {}).get("name", "Unknown")
        created = c.get("created_time", "")
        text = "".join(
            rt.get("plain_text", "") for rt in c.get("rich_text", [])
        )
        disc_id = c.get("discussion_id", "")
        lines.append(f"  [{created}] {author}:")
        lines.append(f"    {text}")
        if disc_id:
            lines.append(f"    discussion_id: {disc_id}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def notion_get_comment(comment_id: str) -> str:
    """Get a single comment by ID — text, author, timestamps and the
    discussion thread it belongs to.

    comment_id: from notion_get_comments or notion_add_comment.
    """
    c = notion.retrieve_comment(comment_id)
    text = "".join(rt.get("plain_text", "") for rt in c.get("rich_text", []))
    parent = c.get("parent", {})
    return (
        f"Comment {c['id']}:\n"
        f"  {text}\n"
        f"  author: {c.get('created_by', {}).get('id', '')}\n"
        f"  created: {c.get('created_time', '')}\n"
        f"  edited: {c.get('last_edited_time', '')}\n"
        f"  discussion_id: {c.get('discussion_id', '')}\n"
        f"  parent: {parent.get(parent.get('type', ''), '')}"
    )


@mcp.tool()
def notion_add_comment(
    page_id: str,
    text: str,
    discussion_id: str = "",
) -> str:
    """Add a comment to a Notion page or reply to a discussion thread.

    page_id:       the page to comment on.
    text:          comment text. Sent as native API markdown — inline
                   formatting, inline equations, and @mentions all work.
    discussion_id: if provided, the comment is a reply to this existing
                   discussion thread. Get discussion IDs from
                   notion_get_comments.
    """
    comment = notion.create_comment(page_id, discussion_id=discussion_id, markdown=text)
    return (
        f"Comment added:\n"
        f"  ID: {comment['id']}\n"
        f"  Discussion: {comment.get('discussion_id', '')}"
    )


@mcp.tool()
def notion_update_comment(comment_id: str, text: str) -> str:
    """Edit a comment previously created by this integration (Notion
    returns 404 for comments created by anyone else).

    comment_id: from notion_get_comments or notion_add_comment.
    text:       replacement text (native API markdown — inline
                formatting, equations, @mentions).
    """
    comment = notion.update_comment(comment_id, markdown=text)
    new_text = "".join(
        rt.get("plain_text", "") for rt in comment.get("rich_text", [])
    )
    return f"Comment {comment_id} updated:\n  {new_text}"


@mcp.tool()
def notion_delete_comment(comment_id: str) -> str:
    """Delete a comment previously created by this integration (Notion
    returns 404 for comments created by anyone else).

    comment_id: from notion_get_comments or notion_add_comment.
    """
    notion.delete_comment(comment_id)
    return f"Comment {comment_id} deleted."


# ---------------------------------------------------------------------------
# Views (2026-03 Views API)
# ---------------------------------------------------------------------------


@mcp.tool()
def notion_list_views(database_id: str) -> str:
    """List the views defined on a Notion database — name, type, id, and
    whether each carries a saved filter/sorts. Use the view id with
    notion_query_view to run a view's saved filter, or with
    notion_update_view / notion_delete_view to manage it.
    """
    views = notion.list_views(database_id)
    if not views:
        return f"No views on database {database_id}."
    lines = [f"{len(views)} view(s) on database {database_id}:\n"]
    for v in views:
        extras = []
        if v.get("filter"):
            extras.append("filtered")
        if v.get("sorts"):
            extras.append("sorted")
        suffix = f" ({', '.join(extras)})" if extras else ""
        lines.append(f"  • {v.get('name', '(unnamed)')} [{v.get('type', '?')}]{suffix}")
        lines.append(f"      id: {v['id']}")
    return "\n".join(lines)


@mcp.tool()
def notion_query_view(view_id: str, limit: int = 20) -> str:
    """Run a database view's saved filter and sorts, returning the matching
    rows (title + id + url). Use this instead of notion_query_database when
    the view already encodes the filter you want — no filter JSON needed.

    view_id: from notion_list_views.
    limit:   max rows to return (max 100).
    """
    result = notion.query_view(view_id, page_size=max(1, min(100, limit)))
    pages = result["results"]
    if not pages:
        return f"View {view_id} matched no rows."
    total = result.get("total_count")
    header = f"{len(pages)} row(s)"
    if total is not None and total > len(pages):
        header += f" of {total} total (raise limit or narrow the view)"
    lines = [f"{header} from view {view_id}:\n"]
    for p in pages:
        lines.append(f"  • {notion.get_page_title(p)}")
        lines.append(f"      id: {p['id']}")
    return "\n".join(lines)


@mcp.tool()
def notion_create_view(
    database_id: str,
    name: str,
    view_type: str = "table",
    filter_json: str = "",
    sorts_json: str = "",
) -> str:
    """Create a view on a Notion database.

    database_id: the database to add the view to.
    name:        display name for the view.
    view_type:   table | board | list | calendar | timeline | gallery |
                 chart (default table).
    filter_json: optional Notion filter object as JSON (same shape as
                 notion_query_database's filter_json).
    sorts_json:  optional JSON array of sort objects.
    """
    filter_obj = json.loads(filter_json) if filter_json else None
    sorts_obj = json.loads(sorts_json) if sorts_json else None
    v = notion.create_view(
        database_id, name, view_type, filter=filter_obj, sorts=sorts_obj,
    )
    return (
        f"View created:\n"
        f"  Name: {v.get('name')}\n"
        f"  Type: {v.get('type')}\n"
        f"  ID:   {v['id']}\n"
        f"  URL:  {v.get('url', '')}"
    )


@mcp.tool()
def notion_update_view(
    view_id: str,
    name: str = "",
    filter_json: str = "",
    sorts_json: str = "",
) -> str:
    """Update a view's name, saved filter, and/or sorts. Only provided
    fields change (fields left empty are preserved).
    """
    filter_obj = json.loads(filter_json) if filter_json else None
    sorts_obj = json.loads(sorts_json) if sorts_json else None
    v = notion.update_view(
        view_id, name=name, filter=filter_obj, sorts=sorts_obj,
    )
    return f"View {view_id} updated: {v.get('name')} [{v.get('type')}]"


@mcp.tool()
def notion_delete_view(view_id: str) -> str:
    """Delete a view from its database. The database and its rows are
    untouched — only the saved view configuration is removed."""
    notion.delete_view(view_id)
    return f"View {view_id} deleted."


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@mcp.tool()
def notion_list_users(name_contains: str = "") -> str:
    """List all users in the Notion workspace.

    Returns user names, IDs, emails, and types (person or bot). User IDs
    are needed for people properties and @mentions in comments.

    name_contains: optional case-insensitive filter on the user's name.
    """
    users = notion.list_users()
    if name_contains:
        users = [
            u for u in users
            if name_contains.lower() in (u.get("name") or "").lower()
        ]
    if not users:
        return "No users found."

    lines = [f"{len(users)} user(s) in workspace:\n"]
    for u in users:
        name = u.get("name", "(unnamed)")
        utype = u.get("type", "")
        email = ""
        if utype == "person":
            email = u.get("person", {}).get("email", "")
        elif utype == "bot":
            email = "(bot)"
        lines.append(f"  {name}")
        lines.append(f"    id: {u['id']}")
        if email:
            lines.append(f"    email: {email}")
        lines.append(f"    type: {utype}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def notion_get_user(user_id: str) -> str:
    """Get details for a specific Notion user by ID.

    Returns the user's name, email, avatar URL, and type. Use
    notion_list_users to find user IDs first.

    user_id: Notion user UUID, or "me" for this integration's own bot
             user (also shows the workspace it is connected to).
    """
    u = notion.get_user(user_id)
    name = u.get("name", "(unnamed)")
    utype = u.get("type", "")
    lines = [f"User: {name}", f"  id: {u['id']}", f"  type: {utype}"]
    if utype == "person":
        person = u.get("person", {})
        if person.get("email"):
            lines.append(f"  email: {person['email']}")
    elif utype == "bot":
        bot = u.get("bot", {})
        lines.append(f"  owner: {bot.get('owner', {}).get('type', 'unknown')}")
        if bot.get("workspace_name"):
            lines.append(f"  workspace: {bot['workspace_name']}")
        max_upload = (bot.get("workspace_limits") or {}).get(
            "max_file_upload_size_in_bytes"
        )
        if max_upload:
            lines.append(f"  max file upload: {max_upload} bytes")
    avatar = u.get("avatar_url")
    if avatar:
        lines.append(f"  avatar: {avatar}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown (native API — 2025-09-03+)
# ---------------------------------------------------------------------------


@mcp.tool()
def notion_read_page_markdown(page_id: str, markdown_version: str = "") -> str:
    """Read a Notion page's content as native Notion-flavored markdown.

    Uses the direct markdown API endpoint — faster than block-by-block
    fetching and returns Notion's own markdown representation including
    special tags for databases, files, and embeds.

    For block-level editing (update/delete individual blocks), use
    notion_read_page with include_block_ids=True instead.

    page_id: Notion page ID.
    markdown_version: "v2" returns Notion-flavored Markdown (a preview
        syntax); default is the stable syntax.
    """
    page = notion.retrieve_page(page_id)
    title = notion.get_page_title(page)
    props = format_page_properties(page.get("properties", {}))
    md = notion.get_page_markdown(page_id, markdown_version=markdown_version)

    parts = [f"# {title}", f"  id: {page_id}", f"  url: {page.get('url', '')}"]
    if props:
        parts.append(f"\nProperties:\n{props}")
    if md:
        parts.append(f"\nContent:\n{md}")
    else:
        parts.append("\n(empty page)")
    return "\n".join(parts)


@mcp.tool()
def notion_update_page_content(
    page_id: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    allow_deleting_content: bool = False,
) -> str:
    """Edit page content using search-and-replace on the page's markdown.

    Finds old_text in the page content and replaces it with new_text.
    Use notion_read_page_markdown first to see the current content and
    copy the exact text to replace.

    page_id:  the page to edit.
    old_text: exact text to find in the page content.
    new_text: replacement text (use empty string to delete).
    replace_all: if old_text occurs more than once the edit is rejected
              unless this is True (then every occurrence is replaced).
    allow_deleting_content: must be True for an edit that would delete a
              child page or database.
    """
    notion.update_page_markdown(
        page_id, old_text, new_text,
        replace_all=replace_all, allow_deleting_content=allow_deleting_content,
    )
    return f"Updated page content on {page_id}."


@mcp.tool()
def notion_insert_page_content(
    page_id: str, markdown: str, position: str = "end", after: str = "",
) -> str:
    """Insert markdown into a page using Notion's own markdown parser —
    at the start, at the end, or after a piece of existing text.

    Unlike notion_append_content (which converts markdown to blocks
    locally), this understands Notion-specific markup (tables, callouts,
    columns, toggles, mentions) and can target a spot by its text.

    page_id:  the page to insert into.
    markdown: content to insert.
    position: "end" (default) or "start". Ignored when after is given.
    after:    insert directly after this existing content, given as
              "start text...end text" (the first and last few words of the
              passage, joined by three dots). Copy them from
              notion_read_page_markdown.
    """
    pos = position.strip().lower()
    if not after and pos not in ("", "start", "end"):
        return 'position must be "start" or "end" (or use after).'
    notion.insert_page_markdown(
        page_id, markdown, position="" if after else pos, after=after,
    )
    where = f"after '{after}'" if after else f"at the {pos or 'end'}"
    return f"Inserted content {where} of page {page_id}."


@mcp.tool()
def notion_replace_page_content(
    page_id: str,
    markdown: str,
    allow_deleting_content: bool = False,
    markdown_version: str = "",
) -> str:
    """Replace a page's entire content with new markdown.

    Overwrites all existing content. Use notion_read_page_markdown first
    to see what's there. For targeted edits, use notion_update_page_content
    instead.

    page_id:  the page to replace content on.
    markdown: new page content in markdown format.
    allow_deleting_content: must be True when the replacement would delete
              a child page or database that is on the page now.
    markdown_version: "v2" treats markdown as Notion-flavored Markdown (a
              preview syntax); default is the stable syntax.

    Very large pages are processed by Notion as a background task; if it
    is still running when this returns, the reply carries a task ID for
    notion_get_async_task.
    """
    res = notion.replace_page_markdown(
        page_id, markdown,
        allow_deleting_content=allow_deleting_content,
        markdown_version=markdown_version,
    )
    return _pending_task_note(res) or f"Replaced all content on page {page_id}."


@mcp.tool()
def notion_get_async_task(task_id: str) -> str:
    """Check a background write that Notion had not finished when the tool
    that started it returned (large notion_create_page /
    notion_replace_page_content calls report a task ID in that case).

    task_id: the task ID from that reply.
    """
    t = notion.get_async_task(task_id)
    status = t.get("status", "unknown")
    op = (t.get("operation") or {}).get("name", "")
    lines = [f"Task {task_id}: {status}", f"  operation: {op}"]
    if status == "succeeded":
        result = t.get("result") or {}
        lines.append(f"  result: {result.get('object', '')} {result.get('id', '')}")
        if result.get("url"):
            lines.append(f"  url: {result['url']}")
    elif status == "failed":
        err = t.get("error") or {}
        lines.append(f"  error: {err.get('code')}: {err.get('message')}")
    else:
        lines.append(
            f"  still running — check again in {t.get('poll_after_seconds', 2)}s"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Move page (2025-09-03+)
# ---------------------------------------------------------------------------


@mcp.tool()
def notion_move_page(
    page_id: str,
    new_parent_page_id: str = "",
    new_parent_database_id: str = "",
) -> str:
    """Move a Notion page to a new parent page or database.

    page_id:                the page to move.
    new_parent_page_id:     move under this page.
    new_parent_database_id: move into this database (as a new row).
                            Only one of the two parent args should be set.
    """
    if new_parent_database_id:
        ds_id = notion.resolve_data_source_id(new_parent_database_id)
        parent = {"data_source_id": ds_id}
    elif new_parent_page_id:
        parent = {"page_id": new_parent_page_id}
    else:
        return "Provide new_parent_page_id or new_parent_database_id."

    notion.move_page(page_id, parent)
    target = new_parent_page_id or new_parent_database_id
    return f"Moved page {page_id} to new parent {target}."


# ---------------------------------------------------------------------------
# Meeting notes, custom emojis, file-upload listing
# ---------------------------------------------------------------------------


def _meeting_notes_lines(b: dict) -> list[str]:
    mn = b.get("meeting_notes", {})
    title = "".join(t.get("plain_text", "") for t in mn.get("title", []))
    lines = [f"  • {title or '(untitled)'} [{mn.get('status', '?')}]",
             f"      id: {b['id']}"]
    start = (mn.get("calendar_event") or mn.get("recording") or {}).get("start_time")
    lines.append(f"      when: {start or b.get('created_time', '')}")
    attendees = (mn.get("calendar_event") or {}).get("attendees") or []
    if attendees:
        lines.append(f"      attendees: {len(attendees)}")
    return lines


@mcp.tool()
def notion_query_meeting_notes(
    title_contains: str = "",
    filter_json: str = "",
    sort_json: str = "",
    limit: int = 10,
) -> str:
    """Find AI meeting notes in the workspace (newest first by default).
    Read one with notion_read_meeting_notes.

    title_contains: simple filter on the meeting title.
    filter_json: full Notion meeting-notes filter instead, e.g.
        {"property": "created_time", "filter": {"date_is_after": "2026-10-01"}}
        or {"operator": "and", "filters": [...]}. Properties: title,
        attendees, created_time, created_by, last_edited_time,
        last_edited_by. Overrides title_contains.
    sort_json: JSON array, e.g.
        [{"property": "created_time", "direction": "descending"}].
    limit: max results (1-50). There is no paging — narrow the filter.
    """
    filter_obj = json.loads(filter_json) if filter_json else None
    if not filter_obj and title_contains:
        filter_obj = {
            "property": "title", "filter": {"string_contains": title_contains},
        }
    sort_obj = json.loads(sort_json) if sort_json else [
        {"property": "created_time", "direction": "descending"},
    ]
    res = notion.query_meeting_notes(filter=filter_obj, sort=sort_obj, limit=limit)
    notes = res.get("results", [])
    if not notes:
        return "No meeting notes found."
    lines = [f"{len(notes)} meeting note(s):\n"]
    for b in notes:
        lines.extend(_meeting_notes_lines(b))
    if res.get("has_more"):
        lines.append("\nMore exist — narrow the filter to see them.")
    return "\n".join(lines)


@mcp.tool()
def notion_read_meeting_notes(block_id: str, section: str = "summary") -> str:
    """Read one section of an AI meeting notes block.

    block_id: the meeting notes block ID (from notion_query_meeting_notes,
              or a "[Meeting notes: …]" line in notion_read_page output).
    section:  "summary" (default), "notes" or "transcript".
    """
    section = section.strip().lower()
    if section not in ("summary", "notes", "transcript"):
        return 'section must be "summary", "notes" or "transcript".'
    block = notion.retrieve_block(block_id)
    if block.get("type") != "meeting_notes":
        return f"Block {block_id} is a '{block.get('type')}' block, not meeting notes."
    mn = block["meeting_notes"]
    title = "".join(t.get("plain_text", "") for t in mn.get("title", []))
    status = mn.get("status", "")
    section_id = (mn.get("children") or {}).get(f"{section}_block_id")
    header = f"# {title or '(untitled)'} — {section}\n  status: {status}"
    if not section_id:
        return f"{header}\n\n(no {section} yet — status is {status})"
    content = blocks_to_markdown(notion.get_block_children_deep(section_id))
    return f"{header}\n\n{content or '(empty)'}"


@mcp.tool()
def notion_create_meeting_notes(
    source: str,
    parent_page_id: str = "",
    title: str = "",
    language: str = "",
    summarize: bool = True,
) -> str:
    """Create AI meeting notes (transcript + summary) from a recording.
    Uses Notion AI; processing continues after this returns — poll with
    notion_read_meeting_notes until its status is notes_ready.

    NOT idempotent: calling it twice creates two sets of notes.

    source: an audio/video file as `shared:<name>`, `local:<path>`,
            `drive:<name-or-id>` — uploaded, then transcribed onto
            parent_page_id — or `notion:<block_id>` to transcribe an audio
            block that is already on a page (no parent_page_id then).
    parent_page_id: page the notes are added to (file sources only).
    title:    meeting title (Notion may refine it when summarising).
    language: transcription hint such as "en" or "ja" (default: auto).
    summarize: False transcribes without generating a summary.
    """
    scheme, _, value = source.partition(":")
    if scheme == "notion":
        src = {"type": "block", "block_id": value.strip()}
        parent_page_id = ""
    else:
        if not parent_page_id:
            return "parent_page_id is required when source is a file."
        data, name, mime = _read_source(source)
        if name.lower().endswith(".m4a"):
            mime = "audio/mp4"  # the API rejects other types for .m4a
        src = {
            "type": "file_upload",
            "file_upload_id": _upload_bytes_to_notion(data, name, mime),
        }
    block = notion.create_meeting_notes(
        src, parent_page_id=parent_page_id, title=title,
        language=language, kickoff_summary=summarize,
    )
    status = block.get("meeting_notes", {}).get("status", "processing")
    return (
        f"Meeting notes created:\n"
        f"  Block ID: {block['id']}\n"
        f"  Status: {status}\n"
        f"Read them with notion_read_meeting_notes once status is notes_ready."
    )


@mcp.tool()
def notion_list_custom_emojis(name: str = "") -> str:
    """List the workspace's custom emojis. Use one as a page icon with
    icon="custom_emoji:<name>" on notion_create_page / notion_update_page.

    name: optional exact-name filter.
    """
    emojis = notion.list_custom_emojis(name=name)
    if not emojis:
        return "No custom emojis found."
    lines = [f"{len(emojis)} custom emoji(s):\n"]
    for e in emojis:
        lines.append(f"  :{e.get('name', '')}:  id: {e['id']}")
    return "\n".join(lines)


@mcp.tool()
def notion_list_file_uploads(status: str = "", limit: int = 20) -> str:
    """List files this integration has uploaded to Notion, with their
    status — useful for checking an import or finding an upload ID.

    status: optional filter: pending | uploaded | expired | failed.
    limit:  max results (1-100).
    """
    res = notion.list_file_uploads(status=status, page_size=limit)
    uploads = res.get("results", [])
    if not uploads:
        return "No file uploads found."
    lines = [f"{len(uploads)} file upload(s):\n"]
    for u in uploads:
        lines.append(f"  • {u.get('filename') or '(unnamed)'} [{u.get('status', '?')}]")
        lines.append(f"      id: {u['id']}")
        lines.append(
            f"      {u.get('content_type') or '?'}, "
            f"{u.get('content_length') or 0} bytes, created {u.get('created_time', '')}"
        )
    if res.get("has_more"):
        lines.append("\nMore exist — filter by status or raise limit.")
    return "\n".join(lines)


def main():
    # Emit our per-request tool logs at INFO so they reach journald → Loki.
    # basicConfig writes to stderr, so this is safe for the stdio transport
    # too (stdout must stay pure JSON-RPC).
    logging.basicConfig(level=logging.INFO)

    serve(mcp, port=PORT, bearer_token=MCP_BEARER_TOKEN)


if __name__ == "__main__":
    main()
