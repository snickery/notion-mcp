"""Notion REST client — pages, databases, blocks, comments, users, files.

Covers the full public Notion API surface:
  - Pages: create, retrieve, update, archive
  - Databases: create, retrieve, query
  - Blocks: list children, append, update, delete
  - Comments: create, list
  - Users: list, retrieve
  - File uploads (3-stage single_part protocol)
  - Search
"""
import logging
import mimetypes
import time

import httpx

from notion_mcp.config import (
    NOTION_API_BASE,
    NOTION_API_VERSION,
    NOTION_TOKEN,
)

logger = logging.getLogger(__name__)


class NotionClient:
    def __init__(self):
        self._ds_cache: dict[str, str] = {}  # database_id -> data_source_id

    def _headers(self, extra: dict | None = None) -> dict:
        if not NOTION_TOKEN:
            raise RuntimeError(
                "NOTION_TOKEN not set. Paste the integration token from "
                "https://www.notion.so/my-integrations into .secrets.env."
            )
        h = {
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": NOTION_API_VERSION,
        }
        if extra:
            h.update(extra)
        return h

    # Notion rate-limits at ~3 req/s and returns 429 with a Retry-After
    # header; 502/503/504 occur under load. Retry these with bounded
    # exponential backoff (honouring Retry-After) instead of failing the
    # tool call. The sleep runs in the worker thread the tool was offloaded
    # to (see server.py), so it never blocks the event loop. Worst-case
    # backoff (~1+2+4s, each capped at 30s) stays well under the client's
    # 240s ceiling.
    # 529 = service_overload (added 2026-06; carries Retry-After like 429).
    _RETRY_STATUSES = (429, 502, 503, 504, 529)
    _MAX_ATTEMPTS = 4

    def _request(self, method: str, path: str, **kw) -> dict:
        url = f"{NOTION_API_BASE}{path}"
        headers = self._headers(kw.pop("headers", None))
        backoff = 1.0
        r = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            with httpx.Client(timeout=60.0) as c:
                r = c.request(method, url, headers=headers, **kw)
            if (r.status_code in self._RETRY_STATUSES
                    and attempt < self._MAX_ATTEMPTS):
                retry_after = r.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else backoff
                except ValueError:
                    delay = backoff
                delay = min(delay, 30.0)
                logger.warning(
                    "Notion %s %s → %s, retry %d/%d after %.1fs",
                    method, path, r.status_code,
                    attempt, self._MAX_ATTEMPTS - 1, delay,
                )
                time.sleep(delay)
                backoff *= 2
                continue
            break
        if r.status_code >= 400:
            raise RuntimeError(
                f"Notion API {method} {path} → {r.status_code}: {r.text}"
            )
        return r.json() if r.content else {}

    # --- Pages -----------------------------------------------------------

    def get_database(self, database_id: str) -> dict:
        return self._request("GET", f"/databases/{database_id}")

    def retrieve_page(self, page_id: str) -> dict:
        return self._request("GET", f"/pages/{page_id}")

    def create_database_row(self, database_id: str, properties: dict) -> dict:
        body = {"parent": {"database_id": database_id}, "properties": properties}
        return self._request("POST", "/pages", json=body)

    def update_page_properties(self, page_id: str, properties: dict) -> dict:
        return self._request(
            "PATCH", f"/pages/{page_id}", json={"properties": properties}
        )

    @staticmethod
    def find_title_property(schema: dict) -> str:
        """Find the title property name from a database or data source dict."""
        for name, p in schema.get("properties", {}).items():
            if p.get("type") == "title":
                return name
        raise ValueError("Schema has no title property.")

    def search_pages(
        self, query: str, page_size: int = 20, in_trash: bool = False,
    ) -> list[dict]:
        filter_obj: dict = {"property": "object", "value": "page"}
        if in_trash:
            # 2026-07 addition: restricts results to trashed pages.
            filter_obj["in_trash"] = True
        body = {
            "query": query,
            "filter": filter_obj,
            "page_size": page_size,
        }
        res = self._request("POST", "/search", json=body)
        return res.get("results", [])

    def get_page_title(self, page: dict) -> str:
        props = page.get("properties", {})
        for prop in props.values():
            if prop.get("type") == "title":
                parts = prop.get("title", [])
                return "".join(p.get("plain_text", "") for p in parts) or "(untitled)"
        # Fall back to any workspace/database title shape
        t = page.get("title", [])
        if t:
            return "".join(p.get("plain_text", "") for p in t) or "(untitled)"
        return "(untitled)"

    def create_page(
        self,
        properties: dict,
        parent_page_id: str = "",
        database_id: str = "",
        children: list[dict] | None = None,
        icon: str = "",
        cover_url: str = "",
    ) -> dict:
        """Create a new page under a page or database."""
        body: dict = {"properties": properties}
        if database_id:
            body["parent"] = {"database_id": database_id}
        elif parent_page_id:
            body["parent"] = {"page_id": parent_page_id}
        else:
            raise ValueError("Either parent_page_id or database_id is required.")
        if children:
            body["children"] = children[:100]  # API limit
        if icon:
            body["icon"] = {"type": "emoji", "emoji": icon}
        if cover_url:
            body["cover"] = {"type": "external", "external": {"url": cover_url}}
        page = self._request("POST", "/pages", json=body)
        # Append remaining children in batches if > 100
        if children and len(children) > 100:
            self._append_batched(page["id"], children[100:])
        return page

    def archive_page(self, page_id: str) -> dict:
        """Archive (soft-delete) a page."""
        return self._request(
            "PATCH", f"/pages/{page_id}", json={"in_trash": True}
        )

    # --- Data sources (2025-09-03+) ---------------------------------------

    def resolve_data_source_id(self, database_id: str) -> str:
        """Get the primary data_source_id for a database (cached)."""
        if database_id in self._ds_cache:
            return self._ds_cache[database_id]
        db = self.get_database(database_id)
        sources = db.get("data_sources", [])
        if not sources:
            raise ValueError(
                f"Database {database_id} has no data sources. "
                f"It may not be shared with the integration."
            )
        ds_id = sources[0]["id"]
        self._ds_cache[database_id] = ds_id
        return ds_id

    def get_data_source(self, data_source_id: str) -> dict:
        """Retrieve a data source's schema and metadata."""
        return self._request("GET", f"/data_sources/{data_source_id}")

    def query_data_source(
        self,
        data_source_id: str,
        filter: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 100,
    ) -> list[dict]:
        """Query a data source, returning matching pages (paginated).

        This is the legacy helper that auto-paginates until ``page_size``
        is reached or the data source is exhausted, and returns a flat
        list. It drops the ``next_cursor`` — use
        :meth:`query_data_source_page` when you need to expose pagination
        to a caller."""
        body: dict = {"page_size": min(page_size, 100)}
        if filter:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts

        results: list[dict] = []
        while True:
            res = self._request(
                "POST", f"/data_sources/{data_source_id}/query", json=body,
            )
            results.extend(res.get("results", []))
            if not res.get("has_more") or len(results) >= page_size:
                break
            body["start_cursor"] = res.get("next_cursor")
            if not body["start_cursor"]:
                break
        return results[:page_size]

    def query_data_source_page(
        self,
        data_source_id: str,
        filter: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 100,
        start_cursor: str = "",
    ) -> dict:
        """Query a single page from a data source and return the raw
        pagination envelope ``{"results", "has_more", "next_cursor"}``.

        Unlike :meth:`query_data_source`, this does not auto-loop. The
        caller is responsible for passing the returned ``next_cursor``
        back in on the next call to walk a large result set. This is
        the right primitive for MCP tools that need to cooperate with
        an LLM over a bounded context budget — the LLM can decide
        whether to page again after seeing the first batch."""
        body: dict = {"page_size": min(max(1, page_size), 100)}
        if filter:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts
        if start_cursor:
            body["start_cursor"] = start_cursor
        return self._request(
            "POST", f"/data_sources/{data_source_id}/query", json=body,
        )

    def update_data_source(
        self,
        data_source_id: str,
        title: str = "",
        description: str = "",
        properties: dict | None = None,
    ) -> dict:
        """Update a data source's title, description, or property schema."""
        body: dict = {}
        if title:
            body["title"] = [{"type": "text", "text": {"content": title}}]
        if description is not None and description != "":
            body["description"] = [
                {"type": "text", "text": {"content": description}}
            ]
        if properties is not None:
            body["properties"] = properties
        if not body:
            raise ValueError("Nothing to update.")
        return self._request("PATCH", f"/data_sources/{data_source_id}", json=body)

    # --- Markdown (2025-09-03+) ------------------------------------------

    def get_page_markdown(self, page_id: str) -> str:
        """Retrieve a page's content as Notion-flavored markdown."""
        res = self._request("GET", f"/pages/{page_id}/markdown")
        return res.get("markdown", "")

    def update_page_markdown(
        self, page_id: str, old_text: str, new_text: str,
    ) -> dict:
        """Update page content via search-and-replace on markdown.

        Uses ``replace_content_range``: finds *old_text* and replaces
        with *new_text*.
        """
        return self._request(
            "PATCH", f"/pages/{page_id}/markdown",
            json={
                "type": "replace_content_range",
                "replace_content_range": {
                    "content_range": old_text,
                    "content": new_text,
                },
            },
        )

    def replace_page_markdown(self, page_id: str, markdown: str) -> dict:
        """Replace a page's entire content with new markdown."""
        return self._request(
            "PATCH", f"/pages/{page_id}/markdown",
            json={
                "type": "replace_content",
                "replace_content": {"new_str": markdown},
            },
        )

    def insert_page_markdown(self, page_id: str, markdown: str) -> dict:
        """Append markdown content to the end of a page."""
        return self._request(
            "PATCH", f"/pages/{page_id}/markdown",
            json={
                "type": "insert_content",
                "insert_content": {"content": markdown},
            },
        )

    # --- Move page (2025-09-03+) -----------------------------------------

    def move_page(
        self, page_id: str, parent: dict,
    ) -> dict:
        """Move a page to a new parent.

        ``parent`` should be one of:
          - ``{"page_id": "..."}`` — move under a page
          - ``{"data_source_id": "..."}`` — move into a database
        """
        return self._request(
            "POST", f"/pages/{page_id}/move", json={"parent": parent},
        )

    # --- Databases -------------------------------------------------------

    def create_database(
        self, parent_page_id: str, title: str, properties: dict
    ) -> dict:
        """Create a new database under a page.

        ``properties`` should be a dict mapping property names to their
        type configs in Notion API format, e.g.::

            {"Name": {"title": {}}, "Status": {"select": {"options": [...]}}}
        """
        body = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": properties,
        }
        return self._request("POST", "/databases", json=body)

    def query_database(
        self,
        database_id: str,
        filter: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 100,
    ) -> list[dict]:
        """Query a database via its data source (2025-09-03 compatible)."""
        ds_id = self.resolve_data_source_id(database_id)
        return self.query_data_source(ds_id, filter, sorts, page_size)

    def restore_page(self, page_id: str) -> dict:
        """Restore a page from trash."""
        return self._request(
            "PATCH", f"/pages/{page_id}", json={"in_trash": False}
        )

    def search_databases(self, query: str, page_size: int = 20) -> list[dict]:
        """Search for databases the integration can access."""
        body = {
            "query": query,
            "filter": {"property": "object", "value": "data_source"},
            "page_size": page_size,
        }
        res = self._request("POST", "/search", json=body)
        return res.get("results", [])

    def update_database(
        self,
        database_id: str,
        title: str = "",
        description: str = "",
        properties: dict | None = None,
    ) -> dict:
        """Update a database's schema via its data source (2025-09-03)."""
        ds_id = self.resolve_data_source_id(database_id)
        return self.update_data_source(ds_id, title, description, properties)

    def retrieve_page_property(
        self, page_id: str, property_id: str, page_size: int = 100,
    ) -> dict:
        """Retrieve a single page property item (with pagination for lists)."""
        params: dict = {"page_size": min(page_size, 100)}
        res = self._request(
            "GET", f"/pages/{page_id}/properties/{property_id}", params=params,
        )
        # For paginated list properties (relation, rollup), collect all pages
        if res.get("object") == "list":
            results = list(res.get("results", []))
            while res.get("has_more"):
                params["start_cursor"] = res.get("next_cursor")
                if not params["start_cursor"]:
                    break
                res = self._request(
                    "GET",
                    f"/pages/{page_id}/properties/{property_id}",
                    params=params,
                )
                results.extend(res.get("results", []))
            return {"object": "list", "type": res.get("type", ""), "results": results}
        return res

    # --- Blocks ----------------------------------------------------------

    def list_children(self, block_id: str, page_size: int = 100) -> list[dict]:
        results: list[dict] = []
        cursor: str | None = None
        while True:
            params = {"page_size": page_size}
            if cursor:
                params["start_cursor"] = cursor
            res = self._request("GET", f"/blocks/{block_id}/children", params=params)
            results.extend(res.get("results", []))
            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")
            if not cursor:
                break
        return results

    def list_file_blocks(self, page_id: str) -> list[dict]:
        """Return file/image/pdf/video/audio blocks on a page with their URLs."""
        out: list[dict] = []
        for block in self.list_children(page_id):
            btype = block.get("type")
            if btype not in ("file", "image", "pdf", "video", "audio"):
                continue
            payload = block.get(btype, {})
            src_type = payload.get("type")  # "file" | "external" | "file_upload"
            url = ""
            if src_type == "file":
                url = payload.get("file", {}).get("url", "")
            elif src_type == "external":
                url = payload.get("external", {}).get("url", "")
            caption_rt = payload.get("caption", [])
            caption = "".join(c.get("plain_text", "") for c in caption_rt)
            # Prefer the actual filename; captions are display text and
            # made files hard to find by name in the listing.
            name = payload.get("name", "")
            if not name and src_type == "file":
                # Hosted files carry the name in the URL's last segment.
                name = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            if not name:
                name = caption
            out.append({
                "block_id": block["id"],
                "block_type": btype,
                "source_type": src_type,
                "url": url,
                "name": name,
                "caption": caption,
            })
        return out

    def fetch_file_block_bytes(self, block_id: str) -> tuple[str, str, bytes]:
        """Return (filename, mime_type, content) for a file/image/pdf block."""
        block = self._request("GET", f"/blocks/{block_id}")
        btype = block.get("type")
        if btype not in ("file", "image", "pdf", "video", "audio"):
            raise ValueError(
                f"Block {block_id} is type '{btype}', not a downloadable file."
            )
        payload = block.get(btype, {})
        src_type = payload.get("type")
        if src_type == "external":
            url = payload.get("external", {}).get("url", "")
        else:
            url = payload.get("file", {}).get("url", "")
        if not url:
            raise ValueError(f"Block {block_id} has no file URL.")

        # Prefer the block's real filename, fall back to the URL basename.
        # Caption is user-facing text, not a filename.
        name = (payload.get("name") or "").strip()
        if not name:
            from urllib.parse import urlparse, unquote
            name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or f"{block_id}.bin"

        with httpx.Client(timeout=120.0, follow_redirects=True) as c:
            r = c.get(url)
        if r.status_code >= 400:
            raise RuntimeError(
                f"Failed to download file from Notion S3 URL: {r.status_code}"
            )
        mime = r.headers.get("content-type", "").split(";")[0].strip() \
            or mimetypes.guess_type(name)[0] or "application/octet-stream"
        return name, mime, r.content

    def get_block_children_deep(
        self, block_id: str, max_depth: int = 3, _depth: int = 0,
    ) -> list[dict]:
        """Recursively fetch block children up to *max_depth*.

        Children are attached to each block under a ``_children`` key so
        ``blocks_to_markdown`` can render the full tree.
        """
        blocks = self.list_children(block_id)
        if _depth >= max_depth:
            return blocks
        for block in blocks:
            if block.get("has_children"):
                block["_children"] = self.get_block_children_deep(
                    block["id"], max_depth, _depth + 1,
                )
                # Brief pause to respect rate limits on deep pages
                if _depth == 0:
                    time.sleep(0.05)
        return blocks

    def append_blocks(self, parent_id: str, children: list[dict]) -> list[dict]:
        """Append child blocks to a page or block (batched at 100)."""
        return self._append_batched(parent_id, children)

    def _append_batched(self, parent_id: str, children: list[dict]) -> list[dict]:
        all_results: list[dict] = []
        for i in range(0, len(children), 100):
            batch = children[i : i + 100]
            res = self._request(
                "PATCH",
                f"/blocks/{parent_id}/children",
                json={"children": batch},
            )
            all_results.extend(res.get("results", []))
        return all_results

    def update_block(self, block_id: str, block_data: dict) -> dict:
        """Update a block's content."""
        return self._request("PATCH", f"/blocks/{block_id}", json=block_data)

    def delete_block(self, block_id: str) -> dict:
        """Delete (archive) a block."""
        return self._request("DELETE", f"/blocks/{block_id}")

    # --- Comments --------------------------------------------------------

    def create_comment(
        self,
        page_id: str,
        rich_text: list[dict] | None = None,
        discussion_id: str = "",
        markdown: str = "",
    ) -> dict:
        """Create a comment on a page or reply to a discussion.

        Exactly one of rich_text / markdown must be provided (the API
        enforces this; markdown supports inline formatting + mentions).
        """
        body: dict = {"markdown": markdown} if markdown else {"rich_text": rich_text}
        if discussion_id:
            body["discussion_id"] = discussion_id
        else:
            body["parent"] = {"page_id": page_id}
        return self._request("POST", "/comments", json=body)

    def update_comment(
        self,
        comment_id: str,
        rich_text: list[dict] | None = None,
        markdown: str = "",
    ) -> dict:
        """Edit a comment this integration created (404 on others')."""
        body: dict = {"markdown": markdown} if markdown else {"rich_text": rich_text}
        return self._request("PATCH", f"/comments/{comment_id}", json=body)

    def delete_comment(self, comment_id: str) -> dict:
        """Delete a comment this integration created (404 on others')."""
        return self._request("DELETE", f"/comments/{comment_id}")

    # --- Views (2026-03 Views API) ----------------------------------------

    def create_view(
        self,
        database_id: str,
        name: str,
        view_type: str = "table",
        filter: dict | None = None,
        sorts: list[dict] | None = None,
        configuration: dict | None = None,
    ) -> dict:
        """Create a view on a database.

        The API requires BOTH database_id and data_source_id (verified
        live 2026-08-05 — the docs imply either); the data source is
        resolved through the same cache the query path uses.
        """
        body: dict = {
            "database_id": database_id,
            "data_source_id": self.resolve_data_source_id(database_id),
            "name": name,
            "type": view_type,
        }
        if filter is not None:
            body["filter"] = filter
        if sorts is not None:
            body["sorts"] = sorts
        if configuration is not None:
            body["configuration"] = configuration
        return self._request("POST", "/views", json=body)

    def retrieve_view(self, view_id: str) -> dict:
        return self._request("GET", f"/views/{view_id}")

    def update_view(
        self,
        view_id: str,
        name: str = "",
        filter: dict | None = None,
        sorts: list[dict] | None = None,
        configuration: dict | None = None,
    ) -> dict:
        """Update a view. Only provided fields are sent."""
        body: dict = {}
        if name:
            body["name"] = name
        if filter is not None:
            body["filter"] = filter
        if sorts is not None:
            body["sorts"] = sorts
        if configuration is not None:
            body["configuration"] = configuration
        if not body:
            raise ValueError("Nothing to update — provide at least one field.")
        return self._request("PATCH", f"/views/{view_id}", json=body)

    def delete_view(self, view_id: str) -> dict:
        return self._request("DELETE", f"/views/{view_id}")

    def list_views(self, database_id: str) -> list[dict]:
        """List a database's views, hydrated.

        The list endpoint returns identity-only objects (verified live
        2026-08-05), so each view is fetched individually for its
        name/type/filter.
        """
        results: list[dict] = []
        params: dict = {"database_id": database_id}
        while True:
            res = self._request("GET", "/views", params=params)
            results.extend(res.get("results", []))
            if not res.get("has_more"):
                break
            params["start_cursor"] = res.get("next_cursor")
            if not params["start_cursor"]:
                break
        return [self.retrieve_view(v["id"]) for v in results]

    def query_view(self, view_id: str, page_size: int = 50) -> dict:
        """Run a view's saved filter/sorts and return one page of rows.

        Creates a cached view query, reads the first page, and
        best-effort deletes the cache (it would expire in ~15 min
        anyway). Returns {total_count, results, has_more}.
        """
        q = self._request(
            "POST", f"/views/{view_id}/queries",
            json={"page_size": min(page_size, 100)},
        )
        try:
            return {
                "total_count": q.get("total_count"),
                "results": q.get("results", []),
                "has_more": q.get("has_more", False),
            }
        finally:
            qid = q.get("id")
            if qid:
                try:
                    self._request(
                        "DELETE", f"/views/{view_id}/queries/{qid}"
                    )
                except Exception:  # noqa: BLE001 - cache cleanup only
                    pass

    def list_comments(self, block_id: str, page_size: int = 100) -> list[dict]:
        """List comments on a block or page."""
        results: list[dict] = []
        params: dict = {"block_id": block_id, "page_size": min(page_size, 100)}
        while True:
            res = self._request("GET", "/comments", params=params)
            results.extend(res.get("results", []))
            if not res.get("has_more") or len(results) >= page_size:
                break
            params["start_cursor"] = res.get("next_cursor")
            if not params["start_cursor"]:
                break
        return results[:page_size]

    # --- Users -----------------------------------------------------------

    def list_users(self, page_size: int = 100) -> list[dict]:
        """List all workspace users."""
        results: list[dict] = []
        params: dict = {"page_size": min(page_size, 100)}
        while True:
            res = self._request("GET", "/users", params=params)
            results.extend(res.get("results", []))
            if not res.get("has_more"):
                break
            params["start_cursor"] = res.get("next_cursor")
            if not params["start_cursor"]:
                break
        return results

    def get_user(self, user_id: str) -> dict:
        return self._request("GET", f"/users/{user_id}")

    # --- File uploads ----------------------------------------------------

    def create_file_upload(
        self,
        filename: str,
        content_type: str | None = None,
        mode: str = "single_part",
        number_of_parts: int | None = None,
        external_url: str | None = None,
    ) -> dict:
        """Stage a file upload. mode is one of:
          single_part  — ≤20 MB, bytes sent in one send_file_upload call
          multi_part   — >20 MB, requires number_of_parts; parts are sent
                         with part_number then sealed by complete_file_upload
          external_url — Notion fetches the (HTTPS, public) URL
                         asynchronously; poll retrieve_file_upload until
                         status == 'uploaded'
        """
        body: dict = {"mode": mode, "filename": filename}
        if mode == "external_url":
            if not external_url:
                raise ValueError("external_url mode requires external_url.")
            body["external_url"] = external_url
        else:
            if content_type:
                body["content_type"] = content_type
        if mode == "multi_part":
            if not number_of_parts:
                raise ValueError("multi_part mode requires number_of_parts.")
            body["number_of_parts"] = number_of_parts
        return self._request("POST", "/file_uploads", json=body)

    def send_file_upload(self, upload_id: str, filename: str,
                         content: bytes, content_type: str,
                         part_number: int | None = None) -> dict:
        url = f"{NOTION_API_BASE}/file_uploads/{upload_id}/send"
        headers = {
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": NOTION_API_VERSION,
        }
        files = {"file": (filename, content, content_type)}
        data = {"part_number": str(part_number)} if part_number else None
        # Cap below the MCP client's 240s ceiling so the server fails (and the
        # caller sees a real error) before the client gives up on a wedged
        # upload. Files >20MB are sent as <=20MB parts, so 180s/part is ample.
        with httpx.Client(timeout=180.0) as c:
            r = c.post(url, headers=headers, files=files, data=data)
        if r.status_code >= 400:
            raise RuntimeError(
                f"Notion file upload send failed: {r.status_code}: {r.text}"
            )
        return r.json()

    def complete_file_upload(self, upload_id: str) -> dict:
        """Seal a multi_part upload after all parts are sent."""
        return self._request("POST", f"/file_uploads/{upload_id}/complete")

    def retrieve_file_upload(self, upload_id: str) -> dict:
        """Fetch upload status — external_url imports are async and move
        pending → uploaded | failed."""
        return self._request("GET", f"/file_uploads/{upload_id}")

    def retrieve_block(self, block_id: str) -> dict:
        return self._request("GET", f"/blocks/{block_id}")

    def attach_file_to_page(self, page_id: str, upload_id: str,
                            filename: str, caption: str = "",
                            block_type: str = "file",
                            position: dict | None = None) -> dict:
        """Append a new file/image/pdf block referencing the uploaded file.

        page_id may be a page ID or any block ID that supports children
        (toggle, column, callout, ...). block_type should be one of: file,
        image, pdf, video, audio — Notion validates the content type
        against the chosen block type, so the caller should pick the right
        one for the MIME. position follows the API shape:
        {"type": "start"} or {"type": "after_block",
        "after_block": {"id": ...}}; None appends at the end.
        """
        caption_rt = []
        if caption:
            caption_rt = [{"type": "text", "text": {"content": caption}}]
        # Notion rejects `name` inside the inner file/pdf/image object for
        # PATCH /blocks/:id/children — the filename is carried by the
        # underlying file_upload record. Keep the signature so callers can
        # still pass `filename` for logging/return values.
        _ = filename
        block = {
            "object": "block",
            "type": block_type,
            block_type: {
                "type": "file_upload",
                "file_upload": {"id": upload_id},
                "caption": caption_rt,
            },
        }
        body: dict = {"children": [block]}
        if position:
            body["position"] = position
        return self._request(
            "PATCH",
            f"/blocks/{page_id}/children",
            json=body,
        )

    def update_file_block(self, block_id: str, block_type: str,
                          upload_id: str, caption: str | None = None,
                          name: str | None = None) -> dict:
        """Replace the file in an existing file/image/pdf/video/audio block
        in place (the block keeps its position). caption=None leaves the
        existing caption untouched; name applies to 'file' blocks only."""
        inner: dict = {"file_upload": {"id": upload_id}}
        if caption is not None:
            inner["caption"] = (
                [{"type": "text", "text": {"content": caption}}]
                if caption else []
            )
        if name and block_type == "file":
            inner["name"] = name[:100]
        return self._request(
            "PATCH", f"/blocks/{block_id}", json={block_type: inner}
        )


def pick_block_type(mime: str) -> str:
    """Map a MIME type to the Notion block type that can hold it."""
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "file"
