"""Integration tests — hit the real Notion API against the sandbox page.

Skipped unless NOTION_TEST_TOKEN is set. These tests catch API version
drift and ensure the full stack works end-to-end.

Run with:
    NOTION_TEST_TOKEN=ntn_... pytest tests/test_integration.py -v

The tests create, read, modify, and clean up their own pages under the
MCP Test Sandbox page. Nothing is left behind on success.
"""

import os
import time

import pytest

from tests.conftest import requires_notion, SANDBOX_PAGE_ID, SANDBOX_DB_ID


# Patch the config module before importing the client, so the client
# picks up the test token.
@pytest.fixture(autouse=True, scope="module")
def _patch_config():
    token = os.environ.get("NOTION_TEST_TOKEN", "")
    if not token:
        pytest.skip("NOTION_TEST_TOKEN not set")
    import notion_mcp.config as cfg
    orig = cfg.NOTION_TOKEN
    cfg.NOTION_TOKEN = token
    yield
    cfg.NOTION_TOKEN = orig


@pytest.fixture(scope="module")
def client():
    from notion_mcp.notion_client import NotionClient
    return NotionClient()


@pytest.fixture
def test_page(client):
    """Create a page, yield its ID, archive it after the test."""
    page = client.create_page(
        properties={"title": [{"text": {"content": "Integration Test Page"}}]},
        parent_page_id=SANDBOX_PAGE_ID,
    )
    page_id = page["id"]
    yield page_id
    try:
        client.archive_page(page_id)
    except Exception:
        pass


# ===================================================================
# Basic connectivity
# ===================================================================

@requires_notion
class TestConnectivity:
    def test_search_pages(self, client):
        results = client.search_pages("sandbox", page_size=5)
        assert len(results) >= 1
        titles = [client.get_page_title(p) for p in results]
        assert any("Sandbox" in t for t in titles)

    def test_list_users(self, client):
        users = client.list_users()
        assert len(users) >= 1

    def test_retrieve_page(self, client):
        page = client.retrieve_page(SANDBOX_PAGE_ID)
        assert page["id"] == SANDBOX_PAGE_ID


# ===================================================================
# Page CRUD
# ===================================================================

@requires_notion
class TestPageCRUD:
    def test_create_read_archive_restore(self, client):
        # Create
        page = client.create_page(
            properties={"title": [{"text": {"content": "CRUD Test"}}]},
            parent_page_id=SANDBOX_PAGE_ID,
        )
        page_id = page["id"]
        assert page_id

        try:
            # Read
            fetched = client.retrieve_page(page_id)
            assert client.get_page_title(fetched) == "CRUD Test"

            # Archive
            client.archive_page(page_id)
            archived = client.retrieve_page(page_id)
            assert archived.get("in_trash") is True

            # Restore
            client.restore_page(page_id)
            restored = client.retrieve_page(page_id)
            assert restored.get("in_trash") is False
        finally:
            try:
                client.archive_page(page_id)
            except Exception:
                pass


# ===================================================================
# Blocks
# ===================================================================

@requires_notion
class TestBlocks:
    def test_append_and_read_blocks(self, client, test_page):
        from notion_mcp.blocks import markdown_to_blocks, blocks_to_markdown

        blocks = markdown_to_blocks("# Test Heading\n\nA paragraph.")
        added = client.append_blocks(test_page, blocks)
        assert len(added) == 2

        children = client.list_children(test_page)
        md = blocks_to_markdown(children)
        assert "# Test Heading" in md
        assert "A paragraph." in md

    def test_update_block(self, client, test_page):
        from notion_mcp.blocks import markdown_to_blocks, md_to_rich_text

        blocks = markdown_to_blocks("Original text")
        added = client.append_blocks(test_page, blocks)
        block_id = added[0]["id"]

        client.update_block(block_id, {
            "paragraph": {"rich_text": md_to_rich_text("Updated text")},
        })

        children = client.list_children(test_page)
        texts = []
        for b in children:
            for rt in b.get(b["type"], {}).get("rich_text", []):
                texts.append(rt.get("plain_text", ""))
        assert "Updated text" in texts

    def test_delete_block(self, client, test_page):
        from notion_mcp.blocks import markdown_to_blocks

        blocks = markdown_to_blocks("Delete me")
        added = client.append_blocks(test_page, blocks)
        block_id = added[0]["id"]

        client.delete_block(block_id)

        children = client.list_children(test_page)
        ids = [b["id"] for b in children]
        assert block_id not in ids


# ===================================================================
# Markdown API (2025-09-03+)
# ===================================================================

@requires_notion
class TestMarkdownAPI:
    def test_read_markdown(self, client, test_page):
        from notion_mcp.blocks import markdown_to_blocks
        client.append_blocks(test_page, markdown_to_blocks("Markdown read test"))

        md = client.get_page_markdown(test_page)
        assert "Markdown read test" in md

    def test_replace_markdown(self, client, test_page):
        from notion_mcp.blocks import markdown_to_blocks
        client.append_blocks(test_page, markdown_to_blocks("Before replace"))

        client.replace_page_markdown(test_page, "After replace")
        md = client.get_page_markdown(test_page)
        assert "After replace" in md
        assert "Before replace" not in md

    def test_search_replace_markdown(self, client, test_page):
        client.replace_page_markdown(test_page, "Hello world")
        client.update_page_markdown(test_page, "Hello world", "Hello **Notion**")
        md = client.get_page_markdown(test_page)
        assert "Hello **Notion**" in md

    def test_insert_markdown(self, client, test_page):
        client.replace_page_markdown(test_page, "First line")
        client.insert_page_markdown(test_page, "\n\nAppended line")
        md = client.get_page_markdown(test_page)
        assert "First line" in md
        assert "Appended line" in md


# ===================================================================
# Data sources / Database queries
# ===================================================================

@requires_notion
class TestDataSources:
    def test_resolve_data_source_id(self, client):
        ds_id = client.resolve_data_source_id(SANDBOX_DB_ID)
        assert ds_id  # non-empty string

    def test_get_data_source(self, client):
        ds_id = client.resolve_data_source_id(SANDBOX_DB_ID)
        ds = client.get_data_source(ds_id)
        assert "properties" in ds
        assert len(ds["properties"]) >= 1

    def test_query_database(self, client):
        rows = client.query_database(SANDBOX_DB_ID, page_size=10)
        assert isinstance(rows, list)
        # Sandbox DB should have at least 1 row
        assert len(rows) >= 1

    def test_search_databases(self, client):
        results = client.search_databases("test", page_size=5)
        assert len(results) >= 1
        # Results are data_source objects
        assert results[0].get("object") == "data_source"


# ===================================================================
# Move page
# ===================================================================

@requires_notion
class TestMovePage:
    def test_move_page_between_parents(self, client):
        # Create two pages
        parent_a = client.create_page(
            properties={"title": [{"text": {"content": "Move Parent A"}}]},
            parent_page_id=SANDBOX_PAGE_ID,
        )
        parent_b = client.create_page(
            properties={"title": [{"text": {"content": "Move Parent B"}}]},
            parent_page_id=SANDBOX_PAGE_ID,
        )
        child = client.create_page(
            properties={"title": [{"text": {"content": "Move Child"}}]},
            parent_page_id=parent_a["id"],
        )

        try:
            # Move child from A to B
            client.move_page(child["id"], {"page_id": parent_b["id"]})
            moved = client.retrieve_page(child["id"])
            assert moved["parent"]["page_id"] == parent_b["id"]
        finally:
            for pid in (child["id"], parent_b["id"], parent_a["id"]):
                try:
                    client.archive_page(pid)
                except Exception:
                    pass


# ===================================================================
# Comments
# ===================================================================

@requires_notion
class TestComments:
    def test_create_and_list_comments(self, client, test_page):
        from notion_mcp.blocks import md_to_rich_text

        rt = md_to_rich_text("Integration test comment")
        comment = client.create_comment(test_page, rt)
        assert comment.get("id")

        comments = client.list_comments(test_page)
        texts = [
            "".join(r.get("plain_text", "") for r in c.get("rich_text", []))
            for c in comments
        ]
        assert "Integration test comment" in texts

    def test_markdown_comment_update_delete(self, client, test_page):
        comment = client.create_comment(test_page, markdown="**md** _probe_")
        cid = comment["id"]
        text = "".join(r.get("plain_text", "") for r in comment.get("rich_text", []))
        assert text == "md probe"

        updated = client.update_comment(cid, markdown="edited probe")
        text = "".join(r.get("plain_text", "") for r in updated.get("rich_text", []))
        assert text == "edited probe"

        client.delete_comment(cid)


# ===================================================================
# Views (2026-03 Views API)
# ===================================================================

@requires_notion
class TestViews:
    def test_view_lifecycle(self, client):
        v = client.create_view(SANDBOX_DB_ID, "integration-view", "table")
        vid = v["id"]
        try:
            assert v["name"] == "integration-view"
            assert v["type"] == "table"

            views = client.list_views(SANDBOX_DB_ID)
            assert vid in [x["id"] for x in views]

            out = client.query_view(vid, page_size=5)
            assert isinstance(out["results"], list)
            assert out["total_count"] is not None

            renamed = client.update_view(vid, name="integration-view-2")
            assert renamed["name"] == "integration-view-2"
        finally:
            client.delete_view(vid)
