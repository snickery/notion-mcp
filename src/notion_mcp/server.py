import base64
import concurrent.futures
import functools
import hashlib
import hmac
import inspect
import json
import logging
import mimetypes
import os
import threading
import time
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image

from notion_mcp import pdf_read
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

logger = logging.getLogger(__name__)

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
sources."""

mcp = FastMCP(
    "notion",
    instructions=INSTRUCTIONS,
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    json_response=True,
)
notion = NotionClient()
drive = DriveClient()


# --- Run blocking tool handlers off the event loop -----------------------
# The MCP SDK executes a synchronous @mcp.tool function INLINE on the single
# asyncio event loop (func_metadata.call_fn_with_arg_validation: the sync
# branch is just `return fn(...)`). Every tool here makes a blocking upstream
# call (httpx.Client requests, plus rate-limit backoff sleeps), so one slow
# call would freeze the loop and stall ALL other requests — creates AND reads
# alike — until it returns, surfacing as the client's 4-minute "MCP server
# unresponsive" timeout.
#
# Wrapping mcp.tool makes each *sync* tool run in a worker thread via
# anyio.to_thread.run_sync, keeping the loop free for concurrent requests.
# functools.wraps + an explicit __signature__ preserve the introspected
# signature, so the generated tool schema is byte-for-byte unchanged (clients
# need no restart). Each call also emits a structured one-line log
# (tool / duration / outcome) for Loki/Grafana so the next stall is visible.
#
# Dedicated request logger: plain single-line output (bypasses FastMCP's
# RichHandler, which wraps lines and would split duration_ms onto a
# continuation line, breaking Loki field extraction). propagate=False keeps
# it off the root handler so the line is emitted exactly once.
req_logger = logging.getLogger("notion_mcp.requests")
if not req_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    req_logger.addHandler(_h)
    req_logger.setLevel(logging.INFO)
    req_logger.propagate = False


def _rss_mib() -> int:
    """Current process RSS in MiB, read cheaply from /proc (no psutil dep) —
    lets Grafana correlate a hang/OOM with the request that triggered it."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * (os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf")
                        else 4096) // (1024 * 1024)
    except Exception:
        return -1


_orig_tool = mcp.tool


def _threaded_tool(*d_args, **d_kwargs):
    register = _orig_tool(*d_args, **d_kwargs)

    def decorator(fn):
        if inspect.iscoroutinefunction(fn):
            return register(fn)

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            outcome = "ok"
            try:
                return await anyio.to_thread.run_sync(
                    functools.partial(fn, *args, **kwargs)
                )
            except Exception:
                outcome = "error"
                raise
            finally:
                req_logger.info(
                    "tool=%s outcome=%s duration_ms=%d rss_mib=%d",
                    fn.__name__, outcome,
                    int((time.perf_counter() - start) * 1000),
                    _rss_mib(),
                )

        wrapper.__signature__ = inspect.signature(fn)
        # Register the threaded wrapper with FastMCP (the runtime calls this),
        # but return the ORIGINAL sync fn so the module-level name stays
        # directly callable — tool-to-tool calls and the unit tests invoke
        # these functions synchronously and must not get a coroutine.
        register(wrapper)
        return fn

    return decorator


mcp.tool = _threaded_tool


class BearerAuthMiddleware:
    """Pure ASGI middleware — compatible with SSE streaming."""

    def __init__(self, app, expected_token: str):
        self.app = app
        self.expected_token = expected_token

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path.startswith("/.well-known/"):
                return await self.app(scope, receive, send)
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()
            if auth.startswith("Bearer "):
                token = auth[7:]
                if hmac.compare_digest(token, self.expected_token):
                    return await self.app(scope, receive, send)
            client_host = scope.get("client", ("unknown",))[0]
            logger.warning("Rejected request: invalid bearer from %s", client_host)
            body = json.dumps({"error": "unauthorized"}).encode()
            await send({"type": "http.response.start", "status": 401, "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


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


def _atomic_create(root: Path, filename: str, data: bytes) -> Path:
    """Create ``filename`` under ``root`` without overwriting: on collision
    auto-rename with -2, -3, ... before the extension. Atomic
    (O_CREAT|O_EXCL) so parallel writers do not clobber each other — same
    semantics as google-accounts-mcp's shared-mount writes, so the two servers
    behave identically. Callers must pass a bare filename."""
    stem, ext = os.path.splitext(filename)
    root.mkdir(parents=True, exist_ok=True)
    for i in range(0, 1001):
        candidate = root / (filename if i == 0 else f"{stem}-{i + 1}{ext}")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as fp:
                fp.write(data)
        except Exception:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
            raise
        return candidate
    raise RuntimeError(
        f"Could not find a free filename for '{filename}' in {root} "
        f"(tried up to -1000)."
    )


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

    ## Cross-MCP file transfer
    To upload a Gmail attachment to a Notion page:
      1. google-accounts-mcp download_attachment(..., destination='shared')
      2. notion_upload_file(source='shared:<filename>', parent_page_id=...)
    Files never traverse MCP parameters — no size limits.
    """
    data, name, mime = _read_source(source, filename)
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
                    property value objects.
    icon:           emoji character for the page icon (e.g. "🚀").
    """
    # Build properties
    if properties_json:
        properties = json.loads(properties_json)
    else:
        properties = {}

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

    children = markdown_to_blocks(content) if content else None

    page = notion.create_page(
        properties=properties,
        parent_page_id=parent_page_id,
        database_id=database_id,
        children=children,
        icon=icon,
    )
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
) -> str:
    """Update a Notion page's title, icon, or other properties.

    page_id:         the page to update.
    title:           new title (leave empty to keep current).
    properties_json: JSON object of properties to update. Keys are property
                     names, values are Notion API property value objects.
                     Omitted properties are left unchanged.
    icon:            emoji for page icon, or "remove" to clear it.
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
    if icon == "remove":
        body["icon"] = None
    elif icon:
        body["icon"] = {"type": "emoji", "emoji": icon}

    if not body:
        return "Nothing to update — provide title, properties_json, or icon."

    notion._request("PATCH", f"/pages/{page_id}", json=body)
    return f"Updated page {page_id}."


@mcp.tool()
def notion_append_content(page_id: str, content: str) -> str:
    """Append markdown content to an existing Notion page.

    Parses the markdown into blocks and appends them to the end of the page.
    Supports: headings (# ## ###), bullet/numbered/to-do lists, quotes,
    fenced code blocks, dividers (---), paragraphs with inline formatting
    (**bold**, *italic*, `code`, ~~strike~~, [links](url)).

    page_id: the page to append to.
    content: markdown text to append.
    """
    if not content.strip():
        return "No content to append."
    blocks = markdown_to_blocks(content)
    added = notion.append_blocks(page_id, blocks)
    return f"Appended {len(added)} block(s) to page {page_id}."


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
    return "\n".join(lines)


@mcp.tool()
def notion_create_database(
    parent_page_id: str,
    title: str,
    schema_json: str,
) -> str:
    """Create a new Notion database under a page.

    parent_page_id: page to create the database under (it appears as an
                    inline database on that page).
    title:          database title.
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
    """
    properties = json.loads(schema_json)
    db = notion.create_database(parent_page_id, title, properties)
    return (
        f"Created database:\n"
        f"  Title: {title}\n"
        f"  ID: {db['id']}\n"
        f"  URL: {db.get('url', '')}"
    )


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
def notion_list_users() -> str:
    """List all users in the Notion workspace.

    Returns user names, IDs, emails, and types (person or bot). User IDs
    are needed for people properties and @mentions in comments.
    """
    users = notion.list_users()
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

    user_id: Notion user UUID.
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
        owner = u.get("bot", {}).get("owner", {})
        lines.append(f"  owner: {owner.get('type', 'unknown')}")
    avatar = u.get("avatar_url")
    if avatar:
        lines.append(f"  avatar: {avatar}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown (native API — 2025-09-03+)
# ---------------------------------------------------------------------------


@mcp.tool()
def notion_read_page_markdown(page_id: str) -> str:
    """Read a Notion page's content as native Notion-flavored markdown.

    Uses the direct markdown API endpoint — faster than block-by-block
    fetching and returns Notion's own markdown representation including
    special tags for databases, files, and embeds.

    For block-level editing (update/delete individual blocks), use
    notion_read_page with include_block_ids=True instead.

    page_id: Notion page ID.
    """
    page = notion.retrieve_page(page_id)
    title = notion.get_page_title(page)
    props = format_page_properties(page.get("properties", {}))
    md = notion.get_page_markdown(page_id)

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
) -> str:
    """Edit page content using search-and-replace on the page's markdown.

    Finds old_text in the page content and replaces it with new_text.
    Use notion_read_page_markdown first to see the current content and
    copy the exact text to replace.

    page_id:  the page to edit.
    old_text: exact text to find in the page content.
    new_text: replacement text (use empty string to delete).
    """
    notion.update_page_markdown(page_id, old_text, new_text)
    return f"Updated page content on {page_id}."


@mcp.tool()
def notion_replace_page_content(page_id: str, markdown: str) -> str:
    """Replace a page's entire content with new markdown.

    Overwrites all existing content. Use notion_read_page_markdown first
    to see what's there. For targeted edits, use notion_update_page_content
    instead.

    page_id:  the page to replace content on.
    markdown: new page content in markdown format.
    """
    notion.replace_page_markdown(page_id, markdown)
    return f"Replaced all content on page {page_id}."


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


def main():
    import sys

    # Emit our per-request tool logs at INFO so they reach journald → Loki.
    # basicConfig writes to stderr, so this is safe for the stdio transport
    # too (stdout must stay pure JSON-RPC).
    logging.basicConfig(level=logging.INFO)

    if "--stdio" in sys.argv:
        # Local single-client mode: the MCP client spawns this process and
        # owns it — no network surface, so no bearer token or uvicorn.
        mcp.run(transport="stdio")
        return

    import uvicorn

    # Streamable HTTP at /mcp, stateless: every tool call is an independent
    # POST, so dropped connections and container restarts never strand a
    # client session. (Legacy SSE was removed 2026-06 once all clients
    # migrated.)
    if not MCP_BEARER_TOKEN:
        raise SystemExit(
            "MCP_BEARER_TOKEN is not set. Refusing to start an unauthenticated "
            "MCP server. Set it in .secrets.env and restart."
        )
    app = BearerAuthMiddleware(mcp.streamable_http_app(), expected_token=MCP_BEARER_TOKEN)
    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
