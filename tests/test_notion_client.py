"""Unit tests for notion_client.py — mock HTTP, test client logic.

Tests pagination, caching, request body construction, error handling.
No real API calls are made.
"""

from unittest.mock import MagicMock, patch

import pytest

from notion_mcp.notion_client import NotionClient, pick_block_type


@pytest.fixture
def client():
    return NotionClient()


@pytest.fixture
def mock_request(client):
    """Patch _request on a client instance and return the mock."""
    with patch.object(client, "_request") as m:
        yield m


# ===================================================================
# pick_block_type
# ===================================================================

class TestPickBlockType:
    def test_pdf(self):
        assert pick_block_type("application/pdf") == "pdf"

    def test_image(self):
        assert pick_block_type("image/png") == "image"
        assert pick_block_type("image/jpeg") == "image"

    def test_video(self):
        assert pick_block_type("video/mp4") == "video"

    def test_audio(self):
        assert pick_block_type("audio/mpeg") == "audio"

    def test_default(self):
        assert pick_block_type("application/zip") == "file"
        assert pick_block_type("text/plain") == "file"


# ===================================================================
# find_title_property
# ===================================================================

class TestFindTitleProperty:
    def test_finds_title(self):
        schema = {"properties": {"Name": {"type": "title"}, "Status": {"type": "select"}}}
        assert NotionClient.find_title_property(schema) == "Name"

    def test_custom_title_name(self):
        schema = {"properties": {"Task": {"type": "title"}}}
        assert NotionClient.find_title_property(schema) == "Task"

    def test_raises_when_missing(self):
        with pytest.raises(ValueError, match="no title"):
            NotionClient.find_title_property({"properties": {"X": {"type": "select"}}})


# ===================================================================
# get_page_title
# ===================================================================

class TestGetPageTitle:
    def test_from_properties(self, client):
        page = {"properties": {"Name": {"type": "title", "title": [{"plain_text": "My Page"}]}}}
        assert client.get_page_title(page) == "My Page"

    def test_fallback_to_title_field(self, client):
        page = {"properties": {}, "title": [{"plain_text": "DB Title"}]}
        assert client.get_page_title(page) == "DB Title"

    def test_untitled(self, client):
        assert client.get_page_title({"properties": {}}) == "(untitled)"

    def test_empty_title_array(self, client):
        page = {"properties": {"T": {"type": "title", "title": []}}}
        assert client.get_page_title(page) == "(untitled)"


# ===================================================================
# resolve_data_source_id
# ===================================================================

class TestResolveDataSourceId:
    def test_resolves_from_database(self, client, mock_request, sample_database):
        mock_request.return_value = sample_database
        assert client.resolve_data_source_id("db-001") == "ds-001"
        mock_request.assert_called_once_with("GET", "/databases/db-001")

    def test_caches_result(self, client, mock_request, sample_database):
        mock_request.return_value = sample_database
        client.resolve_data_source_id("db-001")
        client.resolve_data_source_id("db-001")
        # Only one API call despite two lookups
        assert mock_request.call_count == 1

    def test_raises_when_no_data_sources(self, client, mock_request):
        mock_request.return_value = {"id": "db-x", "data_sources": []}
        with pytest.raises(ValueError, match="no data sources"):
            client.resolve_data_source_id("db-x")


# ===================================================================
# Pagination
# ===================================================================

class TestPagination:
    def test_list_children_single_page(self, client, mock_request):
        mock_request.return_value = {
            "results": [{"id": "b1"}, {"id": "b2"}],
            "has_more": False,
        }
        result = client.list_children("page-1")
        assert len(result) == 2

    def test_list_children_multiple_pages(self, client, mock_request):
        mock_request.side_effect = [
            {"results": [{"id": "b1"}], "has_more": True, "next_cursor": "cur1"},
            {"results": [{"id": "b2"}], "has_more": False},
        ]
        result = client.list_children("page-1")
        assert len(result) == 2
        assert mock_request.call_count == 2

    def test_query_data_source_pagination(self, client, mock_request):
        mock_request.side_effect = [
            {"results": [{"id": "r1"}], "has_more": True, "next_cursor": "c1"},
            {"results": [{"id": "r2"}], "has_more": False},
        ]
        result = client.query_data_source("ds-1", page_size=100)
        assert len(result) == 2

    def test_query_data_source_respects_limit(self, client, mock_request):
        mock_request.return_value = {
            "results": [{"id": f"r{i}"} for i in range(5)],
            "has_more": True,
            "next_cursor": "c1",
        }
        result = client.query_data_source("ds-1", page_size=3)
        assert len(result) == 3
        assert mock_request.call_count == 1  # Stopped after first page

    def test_list_comments_pagination(self, client, mock_request):
        mock_request.side_effect = [
            {"results": [{"id": "c1"}], "has_more": True, "next_cursor": "x"},
            {"results": [{"id": "c2"}], "has_more": False},
        ]
        result = client.list_comments("page-1")
        assert len(result) == 2

    def test_list_users_pagination(self, client, mock_request):
        mock_request.side_effect = [
            {"results": [{"id": "u1"}], "has_more": True, "next_cursor": "x"},
            {"results": [{"id": "u2"}], "has_more": False},
        ]
        result = client.list_users()
        assert len(result) == 2

    def test_query_data_source_page_returns_envelope(
        self, client, mock_request,
    ):
        mock_request.return_value = {
            "results": [{"id": "r1"}, {"id": "r2"}],
            "has_more": True,
            "next_cursor": "c-xyz",
        }
        env = client.query_data_source_page("ds-1", page_size=2)
        assert env["next_cursor"] == "c-xyz"
        assert env["has_more"] is True
        assert len(env["results"]) == 2
        # No second call — single-page semantics.
        assert mock_request.call_count == 1

    def test_query_data_source_page_forwards_start_cursor(
        self, client, mock_request,
    ):
        mock_request.return_value = {"results": [], "has_more": False}
        client.query_data_source_page(
            "ds-1", page_size=10, start_cursor="cursor-1",
        )
        body = mock_request.call_args[1]["json"]
        assert body["start_cursor"] == "cursor-1"
        assert body["page_size"] == 10

    def test_query_data_source_page_omits_empty_cursor(
        self, client, mock_request,
    ):
        mock_request.return_value = {"results": [], "has_more": False}
        client.query_data_source_page("ds-1", page_size=5)
        body = mock_request.call_args[1]["json"]
        assert "start_cursor" not in body


# ===================================================================
# create_page
# ===================================================================

class TestCreatePage:
    def test_with_parent_page(self, client, mock_request):
        mock_request.return_value = {"id": "new-page"}
        client.create_page({"title": []}, parent_page_id="parent-1")
        body = mock_request.call_args[1]["json"]
        assert body["parent"] == {"page_id": "parent-1"}

    def test_with_database(self, client, mock_request):
        mock_request.return_value = {"id": "new-page"}
        client.create_page({"Name": {}}, database_id="db-1")
        body = mock_request.call_args[1]["json"]
        assert body["parent"] == {"database_id": "db-1"}

    def test_requires_parent(self, client):
        with pytest.raises(ValueError, match="Either"):
            client.create_page({"title": []})

    def test_icon_set(self, client, mock_request):
        mock_request.return_value = {"id": "new"}
        client.create_page({"title": []}, parent_page_id="p", icon="🚀")
        body = mock_request.call_args[1]["json"]
        assert body["icon"] == {"type": "emoji", "emoji": "🚀"}

    def test_children_limited_to_100(self, client, mock_request):
        mock_request.return_value = {"id": "new"}
        children = [{"type": "paragraph"} for _ in range(150)]
        client.create_page({"title": []}, parent_page_id="p", children=children)
        # First call: POST /pages with 100 children
        first_body = mock_request.call_args_list[0][1]["json"]
        assert len(first_body["children"]) == 100
        # Second call: PATCH to append remaining 50
        assert mock_request.call_count == 2


# ===================================================================
# Markdown endpoints — request body construction
# ===================================================================

class TestMarkdownEndpoints:
    def test_get_page_markdown(self, client, mock_request):
        mock_request.return_value = {"markdown": "# Hello"}
        result = client.get_page_markdown("page-1")
        assert result == "# Hello"
        mock_request.assert_called_with("GET", "/pages/page-1/markdown")

    def test_update_page_markdown(self, client, mock_request):
        mock_request.return_value = {"object": "page_markdown"}
        client.update_page_markdown("page-1", "old text", "new text")
        body = mock_request.call_args[1]["json"]
        assert body["type"] == "update_content"
        assert body["update_content"] == {
            "content_updates": [{"old_str": "old text", "new_str": "new text"}],
        }

    def test_update_page_markdown_flags(self, client, mock_request):
        mock_request.return_value = {"object": "page_markdown"}
        client.update_page_markdown(
            "page-1", "a", "b", replace_all=True, allow_deleting_content=True,
        )
        cmd = mock_request.call_args[1]["json"]["update_content"]
        assert cmd["content_updates"][0]["replace_all_matches"] is True
        assert cmd["allow_deleting_content"] is True

    def test_get_page_markdown_v2(self, client, mock_request):
        mock_request.return_value = {"markdown": "x"}
        client.get_page_markdown("page-1", markdown_version="v2")
        assert mock_request.call_args[1]["params"] == {"markdown_version": "v2"}

    def test_replace_page_markdown_is_async_capable(self, client, mock_request):
        mock_request.return_value = {"object": "page_markdown"}
        client.replace_page_markdown(
            "page-1", "x", allow_deleting_content=True, markdown_version="v2",
        )
        body = mock_request.call_args[1]["json"]
        assert body["allow_async"] is True
        assert body["markdown_version"] == "v2"
        assert body["replace_content"]["allow_deleting_content"] is True

    @pytest.mark.parametrize("kwargs, expected", [
        ({"position": "start"}, {"content": "x", "position": {"type": "start"}}),
        ({"position": "end"}, {"content": "x", "position": {"type": "end"}}),
        ({"after": "a...b"}, {"content": "x", "after": "a...b"}),
        ({}, {"content": "x"}),
    ])
    def test_insert_page_markdown_placement(self, client, mock_request, kwargs, expected):
        mock_request.return_value = {"object": "page_markdown"}
        client.insert_page_markdown("page-1", "x", **kwargs)
        assert mock_request.call_args[1]["json"]["insert_content"] == expected

    def test_insert_page_markdown_rejects_both(self, client, mock_request):
        with pytest.raises(ValueError, match="not both"):
            client.insert_page_markdown("page-1", "x", position="start", after="a...b")

    def test_replace_page_markdown(self, client, mock_request):
        mock_request.return_value = {"object": "page_markdown"}
        client.replace_page_markdown("page-1", "# New content")
        body = mock_request.call_args[1]["json"]
        assert body["type"] == "replace_content"
        assert body["replace_content"]["new_str"] == "# New content"

    def test_insert_page_markdown(self, client, mock_request):
        mock_request.return_value = {"object": "page_markdown"}
        client.insert_page_markdown("page-1", "appended text")
        body = mock_request.call_args[1]["json"]
        assert body["type"] == "insert_content"
        assert body["insert_content"]["content"] == "appended text"


# ===================================================================
# Move page — request body construction
# ===================================================================

class TestMovePage:
    def test_move_to_page(self, client, mock_request):
        mock_request.return_value = {"object": "page"}
        client.move_page("page-1", {"page_id": "target-1"})
        mock_request.assert_called_with(
            "POST", "/pages/page-1/move",
            json={"parent": {"page_id": "target-1"}},
        )

    def test_move_to_data_source(self, client, mock_request):
        mock_request.return_value = {"object": "page"}
        client.move_page("page-1", {"data_source_id": "ds-1"})
        body = mock_request.call_args[1]["json"]
        assert body["parent"]["data_source_id"] == "ds-1"


# ===================================================================
# query_database delegates to data source
# ===================================================================

class TestQueryDatabase:
    def test_resolves_data_source_then_queries(self, client, mock_request, sample_database):
        mock_request.side_effect = [
            sample_database,  # resolve_data_source_id -> GET /databases/db-001
            {"results": [{"id": "row-1"}], "has_more": False},  # query
        ]
        result = client.query_database("db-001")
        assert len(result) == 1
        # First call resolves DS, second queries it
        assert mock_request.call_args_list[1][0] == ("POST", "/data_sources/ds-001/query")


# ===================================================================
# Comments
# ===================================================================

class TestComments:
    def test_create_comment_page_level(self, client, mock_request):
        mock_request.return_value = {"id": "comment-1"}
        client.create_comment("page-1", [{"type": "text", "text": {"content": "hi"}}])
        body = mock_request.call_args[1]["json"]
        assert body["parent"] == {"page_id": "page-1"}
        assert "discussion_id" not in body

    def test_create_comment_reply(self, client, mock_request):
        mock_request.return_value = {"id": "comment-2"}
        client.create_comment("page-1", [{"type": "text", "text": {"content": "re"}}],
                              discussion_id="disc-1")
        body = mock_request.call_args[1]["json"]
        assert body["discussion_id"] == "disc-1"
        assert "parent" not in body

    def test_create_comment_markdown(self, client, mock_request):
        mock_request.return_value = {"id": "comment-3"}
        client.create_comment("page-1", markdown="**bold** text")
        body = mock_request.call_args[1]["json"]
        assert body["markdown"] == "**bold** text"
        assert "rich_text" not in body
        assert body["parent"] == {"page_id": "page-1"}

    def test_update_comment_markdown(self, client, mock_request):
        mock_request.return_value = {"id": "comment-1"}
        client.update_comment("comment-1", markdown="edited")
        method, path = mock_request.call_args[0]
        assert (method, path) == ("PATCH", "/comments/comment-1")
        assert mock_request.call_args[1]["json"] == {"markdown": "edited"}

    def test_delete_comment(self, client, mock_request):
        mock_request.return_value = {"id": "comment-1"}
        client.delete_comment("comment-1")
        assert mock_request.call_args[0] == ("DELETE", "/comments/comment-1")


# ===================================================================
# Views
# ===================================================================

class TestViews:
    def test_create_view_resolves_data_source(self, client, mock_request):
        # First call resolves the DS (the API requires BOTH ids), second creates.
        mock_request.side_effect = [
            {"data_sources": [{"id": "ds-001"}]},
            {"id": "view-1", "name": "My view", "type": "table"},
        ]
        client.create_view("db-1", "My view")
        method, path = mock_request.call_args[0]
        assert (method, path) == ("POST", "/views")
        body = mock_request.call_args[1]["json"]
        assert body["database_id"] == "db-1"
        assert body["data_source_id"] == "ds-001"
        assert body["type"] == "table"
        assert "filter" not in body

    def test_create_view_with_filter_and_sorts(self, client, mock_request):
        mock_request.side_effect = [
            {"data_sources": [{"id": "ds-001"}]},
            {"id": "view-1"},
        ]
        f = {"property": "Status", "select": {"equals": "Done"}}
        srt = [{"property": "Due", "direction": "ascending"}]
        client.create_view("db-1", "V", "board", filter=f, sorts=srt)
        body = mock_request.call_args[1]["json"]
        assert body["filter"] == f and body["sorts"] == srt
        assert body["type"] == "board"

    def test_update_view_only_sends_provided(self, client, mock_request):
        mock_request.return_value = {"id": "view-1"}
        client.update_view("view-1", name="Renamed")
        assert mock_request.call_args[0] == ("PATCH", "/views/view-1")
        assert mock_request.call_args[1]["json"] == {"name": "Renamed"}

    def test_update_view_nothing_raises(self, client, mock_request):
        import pytest
        with pytest.raises(ValueError, match="Nothing to update"):
            client.update_view("view-1")

    def test_list_views_hydrates(self, client, mock_request):
        # List returns identity-only objects; each is then retrieved.
        mock_request.side_effect = [
            {"results": [{"id": "v1"}, {"id": "v2"}], "has_more": False},
            {"id": "v1", "name": "A", "type": "table"},
            {"id": "v2", "name": "B", "type": "board"},
        ]
        views = client.list_views("db-1")
        assert [v["name"] for v in views] == ["A", "B"]
        assert mock_request.call_args_list[1][0] == ("GET", "/views/v1")

    def test_query_view_cleans_up_cache(self, client, mock_request):
        mock_request.side_effect = [
            {"object": "view_query", "id": "q1", "total_count": 2,
             "results": [{"id": "p1"}, {"id": "p2"}], "has_more": False},
            {"object": "view_query", "id": "q1", "deleted": True},
        ]
        out = client.query_view("view-1", page_size=10)
        assert out["total_count"] == 2 and len(out["results"]) == 2
        assert mock_request.call_args_list[0][0] == ("POST", "/views/view-1/queries")
        assert mock_request.call_args_list[1][0] == ("DELETE", "/views/view-1/queries/q1")

    def test_query_view_survives_cleanup_failure(self, client, mock_request):
        mock_request.side_effect = [
            {"object": "view_query", "id": "q1", "total_count": 0,
             "results": [], "has_more": False},
            RuntimeError("cache already expired"),
        ]
        out = client.query_view("view-1")
        assert out["results"] == []


# ===================================================================
# Search in_trash
# ===================================================================

class TestSearchTrash:
    def test_default_excludes_trash_key(self, client, mock_request):
        mock_request.return_value = {"results": []}
        client.search_pages("q")
        body = mock_request.call_args[1]["json"]
        assert "in_trash" not in body["filter"]

    def test_in_trash_flag(self, client, mock_request):
        mock_request.return_value = {"results": []}
        client.search_pages("q", in_trash=True)
        body = mock_request.call_args[1]["json"]
        assert body["filter"]["in_trash"] is True


# ===================================================================
# _append_batched
# ===================================================================

class TestAppendBatched:
    def test_single_batch(self, client, mock_request):
        mock_request.return_value = {"results": [{"id": "b1"}]}
        result = client.append_blocks("page-1", [{"type": "paragraph"}] * 50)
        assert mock_request.call_count == 1

    def test_multiple_batches(self, client, mock_request):
        mock_request.return_value = {"results": []}
        client.append_blocks("page-1", [{"type": "paragraph"}] * 250)
        assert mock_request.call_count == 3  # 100 + 100 + 50


# ===================================================================
# Archive / Restore — 2026-03-11 uses in_trash instead of archived
# ===================================================================

class TestArchiveRestore:
    def test_archive_sends_in_trash_true(self, client, mock_request):
        mock_request.return_value = {"id": "page-1", "in_trash": True}
        client.archive_page("page-1")
        body = mock_request.call_args[1]["json"]
        assert body == {"in_trash": True}
        assert "archived" not in body

    def test_restore_sends_in_trash_false(self, client, mock_request):
        mock_request.return_value = {"id": "page-1", "in_trash": False}
        client.restore_page("page-1")
        body = mock_request.call_args[1]["json"]
        assert body == {"in_trash": False}
        assert "archived" not in body


# ===================================================================
# attach_file_to_page — regression: Notion rejects `name` inside
# the inner pdf/file/image object on PATCH /blocks/:id/children.
# ===================================================================

class TestAttachFileToPage:
    def test_block_has_no_name_field(self, client, mock_request):
        mock_request.return_value = {"results": []}
        client.attach_file_to_page(
            "page-1", "upload-1", "report.pdf", block_type="pdf",
        )
        body = mock_request.call_args[1]["json"]
        block = body["children"][0]
        assert block["type"] == "pdf"
        inner = block["pdf"]
        assert "name" not in inner, (
            "Notion rejects `name` inside pdf/file/image block payloads"
        )
        assert inner["type"] == "file_upload"
        assert inner["file_upload"] == {"id": "upload-1"}
        assert inner["caption"] == []

    def test_caption_is_rendered(self, client, mock_request):
        mock_request.return_value = {"results": []}
        client.attach_file_to_page(
            "page-1", "upload-1", "x.pdf", caption="hello", block_type="pdf",
        )
        inner = mock_request.call_args[1]["json"]["children"][0]["pdf"]
        assert inner["caption"] == [
            {"type": "text", "text": {"content": "hello"}}
        ]

    def test_file_block_type(self, client, mock_request):
        mock_request.return_value = {"results": []}
        client.attach_file_to_page(
            "page-1", "upload-1", "data.bin", block_type="file",
        )
        block = mock_request.call_args[1]["json"]["children"][0]
        assert block["type"] == "file"
        assert "name" not in block["file"]


class TestRequestRetry:
    """Tier 2: bounded 429/5xx retry with backoff inside _request.

    Patches the httpx layer (and time.sleep so the test is instant) — never
    hits the real Notion API. Regression guard for the event-loop-stall fix:
    rate-limit bursts must retry instead of failing, and the retry budget is
    bounded so a wedged endpoint can't exceed the client's 240s ceiling.
    """

    @staticmethod
    def _resp(status, headers=None, payload=None):
        r = MagicMock()
        r.status_code = status
        r.headers = headers or {}
        r.content = b'{"ok": 1}'
        r.json.return_value = payload if payload is not None else {}
        r.text = "error body"
        return r

    @staticmethod
    def _client_cm(responses):
        mock_client = MagicMock()
        mock_client.request.side_effect = responses
        cm = MagicMock()
        cm.__enter__.return_value = mock_client
        cm.__exit__.return_value = False
        return cm, mock_client

    def test_retries_429_then_succeeds(self, client):
        cm, mock_client = self._client_cm([
            self._resp(429, {"Retry-After": "0"}),
            self._resp(200, payload={"ok": 1}),
        ])
        with patch("notion_mcp.notion_client.NOTION_TOKEN", "tkn"), \
                patch("notion_mcp.notion_client.httpx.Client", return_value=cm), \
                patch("notion_mcp.notion_client.time.sleep") as slept:
            out = client._request("GET", "/pages/x")
        assert out == {"ok": 1}
        assert mock_client.request.call_count == 2
        slept.assert_called_once()

    def test_retries_503(self, client):
        cm, mock_client = self._client_cm([
            self._resp(503),
            self._resp(200, payload={"ok": 1}),
        ])
        with patch("notion_mcp.notion_client.NOTION_TOKEN", "tkn"), \
                patch("notion_mcp.notion_client.httpx.Client", return_value=cm), \
                patch("notion_mcp.notion_client.time.sleep"):
            out = client._request("GET", "/pages/x")
        assert out == {"ok": 1}
        assert mock_client.request.call_count == 2

    def test_gives_up_after_max_attempts(self, client):
        cm, mock_client = self._client_cm(
            [self._resp(429, {"Retry-After": "0"})] * NotionClient._MAX_ATTEMPTS
        )
        with patch("notion_mcp.notion_client.NOTION_TOKEN", "tkn"), \
                patch("notion_mcp.notion_client.httpx.Client", return_value=cm), \
                patch("notion_mcp.notion_client.time.sleep"):
            with pytest.raises(RuntimeError):
                client._request("GET", "/pages/x")
        assert mock_client.request.call_count == NotionClient._MAX_ATTEMPTS

    def test_no_retry_on_400(self, client):
        cm, mock_client = self._client_cm([self._resp(400)])
        with patch("notion_mcp.notion_client.NOTION_TOKEN", "tkn"), \
                patch("notion_mcp.notion_client.httpx.Client", return_value=cm), \
                patch("notion_mcp.notion_client.time.sleep") as slept:
            with pytest.raises(RuntimeError):
                client._request("GET", "/pages/x")
        assert mock_client.request.call_count == 1
        slept.assert_not_called()

    def test_backoff_capped_at_30s(self, client):
        cm, _ = self._client_cm([
            self._resp(429, {"Retry-After": "9999"}),
            self._resp(200, payload={"ok": 1}),
        ])
        with patch("notion_mcp.notion_client.NOTION_TOKEN", "tkn"), \
                patch("notion_mcp.notion_client.httpx.Client", return_value=cm), \
                patch("notion_mcp.notion_client.time.sleep") as slept:
            client._request("GET", "/pages/x")
        assert slept.call_args[0][0] <= 30.0


# ===================================================================
# 2026-10 gap closure: async tasks, notifications, templates, typed
# databases, data sources, meeting notes, emojis, uploads, retries
# ===================================================================

class TestAsyncTasks:
    def test_sync_response_passes_through(self, client, mock_request):
        page = {"object": "page", "id": "p1"}
        assert client.wait_async(page) is page
        mock_request.assert_not_called()

    def test_polls_until_succeeded(self, client, mock_request):
        mock_request.side_effect = [
            {"object": "async_task", "id": "t1", "status": "running",
             "poll_after_seconds": 0},
            {"object": "async_task", "id": "t1", "status": "succeeded",
             "result": {"object": "page", "id": "p1"}},
        ]
        with patch("notion_mcp.notion_client.time.sleep"):
            res = client.wait_async(
                {"object": "async_task", "id": "t1", "status": "queued",
                 "poll_after_seconds": 0},
            )
        assert res == {"object": "page", "id": "p1"}
        mock_request.assert_called_with("GET", "/async_tasks/t1")

    def test_failed_task_raises(self, client, mock_request):
        mock_request.return_value = {
            "object": "async_task", "id": "t1", "status": "failed",
            "error": {"code": "validation_error", "message": "bad"},
        }
        with patch("notion_mcp.notion_client.time.sleep"), \
             pytest.raises(RuntimeError, match="validation_error: bad"):
            client.wait_async(
                {"object": "async_task", "id": "t1", "status": "queued",
                 "poll_after_seconds": 0},
            )

    def test_timeout_returns_pending_task(self, client, mock_request):
        task = {"object": "async_task", "id": "t1", "status": "queued",
                "poll_after_seconds": 5}
        assert client.wait_async(task, timeout=1) is task
        mock_request.assert_not_called()


class TestCreatePageOptions:
    def test_silent_template_position_icon(self, client, mock_request):
        mock_request.return_value = {"id": "p1"}
        client.create_page(
            properties={}, database_id="db",
            icon={"type": "icon", "icon": {"name": "pizza"}},
            template={"type": "default"},
            position={"type": "page_start"}, silent=True,
        )
        body = mock_request.call_args[1]["json"]
        assert body["notifications"] == {"mode": "silent"}
        assert body["template"] == {"type": "default"}
        assert body["position"] == {"type": "page_start"}
        assert body["icon"] == {"type": "icon", "icon": {"name": "pizza"}}
        assert "children" not in body and "markdown" not in body

    def test_native_markdown_uses_allow_async(self, client, mock_request):
        mock_request.return_value = {"object": "page", "id": "p1"}
        client.create_page(
            properties={}, parent_page_id="pp", markdown="# x", markdown_version="v2",
        )
        body = mock_request.call_args[1]["json"]
        assert body["markdown"] == "# x"
        assert body["allow_async"] is True
        assert body["markdown_version"] == "v2"

    def test_template_excludes_children(self, client, mock_request):
        with pytest.raises(ValueError, match="template"):
            client.create_page(
                properties={}, database_id="db",
                children=[{"type": "paragraph"}], template={"type": "default"},
            )

    def test_update_page_silent(self, client, mock_request):
        client.update_page("p1", {"icon": None}, silent=True)
        assert mock_request.call_args[1]["json"] == {
            "icon": None, "notifications": {"mode": "silent"},
        }


class TestAppendBlocksPlacement:
    def test_position_and_silent_sent(self, client, mock_request):
        mock_request.return_value = {"results": [{"id": "b1"}]}
        client.append_blocks(
            "p1", [{"type": "paragraph"}], position={"type": "start"}, silent=True,
        )
        body = mock_request.call_args[1]["json"]
        assert body["position"] == {"type": "start"}
        assert body["notifications"] == {"mode": "silent"}

    def test_later_batches_follow_the_positioned_first(self, client, mock_request):
        mock_request.side_effect = [
            {"results": [{"id": f"a{i}"} for i in range(100)]},
            {"results": [{"id": "z"}]},
        ]
        client.append_blocks(
            "p1", [{"type": "paragraph"}] * 101, position={"type": "start"},
        )
        second = mock_request.call_args_list[1][1]["json"]
        assert second["position"] == {
            "type": "after_block", "after_block": {"id": "a99"},
        }

    def test_plain_append_sends_no_position(self, client, mock_request):
        mock_request.return_value = {"results": []}
        client.append_blocks("p1", [{"type": "paragraph"}])
        assert set(mock_request.call_args[1]["json"]) == {"children"}


class TestDatabaseVariants:
    def test_schema_goes_in_initial_data_source(self, client, mock_request):
        client.create_database("pp", "T", {"Name": {"title": {}}}, is_inline=True)
        body = mock_request.call_args[1]["json"]
        assert body["initial_data_source"] == {"properties": {"Name": {"title": {}}}}
        assert body["is_inline"] is True
        assert "database_type" not in body

    def test_typed_database(self, client, mock_request):
        client.create_database("pp", database_type="tasks")
        body = mock_request.call_args[1]["json"]
        assert body["database_type"] == "tasks"
        assert "initial_data_source" not in body and "title" not in body

    def test_schema_and_type_are_exclusive(self, client, mock_request):
        with pytest.raises(ValueError):
            client.create_database("pp", "T", {"Name": {"title": {}}}, database_type="tasks")
        with pytest.raises(ValueError):
            client.create_database("pp", "T")

    def test_create_data_source(self, client, mock_request):
        client.create_data_source("db1", "Second", {"Name": {"title": {}}})
        mock_request.assert_called_once()
        assert mock_request.call_args[0] == ("POST", "/data_sources")
        assert mock_request.call_args[1]["json"]["parent"] == {
            "type": "database_id", "database_id": "db1",
        }

    def test_list_templates_paginates(self, client, mock_request):
        mock_request.side_effect = [
            {"templates": [{"id": "t1"}], "has_more": True, "next_cursor": "c"},
            {"templates": [{"id": "t2"}], "has_more": False, "next_cursor": None},
        ]
        assert [t["id"] for t in client.list_templates("ds", name="wk")] == ["t1", "t2"]
        assert mock_request.call_args_list[0][1]["params"]["name"] == "wk"
        assert mock_request.call_args_list[1][1]["params"]["start_cursor"] == "c"


class TestQueryOptions:
    def test_is_archived(self, client, mock_request):
        client.query_data_source_page("ds", is_archived=True)
        assert mock_request.call_args[1]["json"]["is_archived"] is True
        assert mock_request.call_args[1]["headers"] is None

    def test_status_group_filter_sends_beta_header(self, client, mock_request):
        f = {"and": [{"property": "S", "status": {"group_equals": "Complete"}}]}
        client.query_data_source_page("ds", filter=f)
        assert mock_request.call_args[1]["headers"] == {
            "Notion-Beta": "status-group-filters-2026-10-06",
        }

    def test_ordinary_filter_sends_no_beta_header(self, client, mock_request):
        client.query_data_source_page(
            "ds", filter={"property": "S", "status": {"equals": "Done"}},
        )
        assert mock_request.call_args[1]["headers"] is None


class TestMeetingNotesAndLists:
    def test_query_meeting_notes_clamps_limit(self, client, mock_request):
        client.query_meeting_notes(limit=500, sort=[{"property": "title"}])
        assert mock_request.call_args[0] == ("POST", "/blocks/meeting_notes/query")
        assert mock_request.call_args[1]["json"] == {
            "limit": 50, "sort": [{"property": "title"}],
        }

    def test_create_meeting_notes_is_not_retried(self, client):
        """Not idempotent: a 529 must surface, not be retried into a duplicate."""
        resp = MagicMock(status_code=529, text="overloaded")
        with patch("notion_mcp.notion_client.httpx.Client") as cls, \
             patch("notion_mcp.notion_client.NOTION_TOKEN", "t"):
            post = cls.return_value.__enter__.return_value.post
            post.return_value = resp
            with pytest.raises(RuntimeError, match="529"):
                client.create_meeting_notes(
                    {"type": "file_upload", "file_upload_id": "u"},
                    parent_page_id="p", title="T", kickoff_summary=False,
                )
        assert post.call_count == 1
        body = post.call_args[1]["json"]
        assert body["parent"] == {"type": "page_id", "page_id": "p"}
        assert body["options"] == {"kickoff_summary": False}

    def test_list_custom_emojis_name_filter(self, client, mock_request):
        mock_request.return_value = {"results": [{"id": "e1"}], "has_more": False}
        assert client.list_custom_emojis(name="party") == [{"id": "e1"}]
        assert mock_request.call_args[1]["params"]["name"] == "party"

    def test_list_file_uploads(self, client, mock_request):
        client.list_file_uploads(status="failed", page_size=500)
        assert mock_request.call_args[1]["params"] == {
            "page_size": 100, "status": "failed",
        }

    def test_attach_html_embed(self, client, mock_request):
        client.attach_html_embed("p1", "u1", position={"type": "start"})
        body = mock_request.call_args[1]["json"]
        assert body["children"][0]["embed"] == {
            "type": "file_upload", "file_upload": {"id": "u1"},
        }
        assert body["position"] == {"type": "start"}


class TestRetryAfterBody:
    def test_body_retry_after_used_when_header_absent(self, client):
        limited = MagicMock(status_code=429, headers={}, content=b"x")
        limited.json.return_value = {"additional_data": {"retry_after": 7}}
        ok = MagicMock(status_code=200, headers={}, content=b"{}")
        ok.json.return_value = {"ok": True}
        with patch("notion_mcp.notion_client.httpx.Client") as cls, \
             patch("notion_mcp.notion_client.NOTION_TOKEN", "t"), \
             patch("notion_mcp.notion_client.time.sleep") as sleep:
            cls.return_value.__enter__.return_value.request.side_effect = [limited, ok]
            assert client._request("GET", "/x") == {"ok": True}
        sleep.assert_called_once_with(7.0)
