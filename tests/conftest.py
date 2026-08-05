"""Shared fixtures for notion-mcp tests."""

import os

import pytest


# ---------------------------------------------------------------------------
# Notion API block / property fixtures
# ---------------------------------------------------------------------------

def _rt(text, bold=False, italic=False, code=False, strikethrough=False, href=None):
    """Build a Notion rich_text element."""
    rt = {
        "type": "text",
        "text": {"content": text},
        "plain_text": text,
        "annotations": {
            "bold": bold,
            "italic": italic,
            "code": code,
            "strikethrough": strikethrough,
            "underline": False,
            "color": "default",
        },
        "href": href,
    }
    return rt


def make_block(block_type, rich_text=None, block_id="block-001", **extra):
    """Build a minimal Notion block dict."""
    payload = dict(extra)
    if rich_text is not None:
        payload["rich_text"] = rich_text
    return {
        "id": block_id,
        "type": block_type,
        "has_children": False,
        block_type: payload,
    }


@pytest.fixture
def paragraph_block():
    return make_block("paragraph", [_rt("Hello world")], block_id="p-001")


@pytest.fixture
def heading_blocks():
    return [
        make_block("heading_1", [_rt("H1 Title")], block_id="h1-001"),
        make_block("heading_2", [_rt("H2 Title")], block_id="h2-001"),
        make_block("heading_3", [_rt("H3 Title")], block_id="h3-001"),
    ]


@pytest.fixture
def list_blocks():
    return [
        make_block("bulleted_list_item", [_rt("Bullet A")], block_id="bl-001"),
        make_block("bulleted_list_item", [_rt("Bullet B")], block_id="bl-002"),
        make_block("numbered_list_item", [_rt("First")], block_id="nl-001"),
        make_block("numbered_list_item", [_rt("Second")], block_id="nl-002"),
    ]


@pytest.fixture
def todo_blocks():
    return [
        make_block("to_do", [_rt("Done task")], block_id="td-001", checked=True),
        make_block("to_do", [_rt("Open task")], block_id="td-002", checked=False),
    ]


@pytest.fixture
def code_block():
    return make_block(
        "code",
        [_rt('print("hello")')],
        block_id="code-001",
        language="python",
    )


@pytest.fixture
def quote_block():
    return make_block("quote", [_rt("Wise words")], block_id="q-001")


@pytest.fixture
def divider_block():
    return {"id": "div-001", "type": "divider", "has_children": False, "divider": {}}


@pytest.fixture
def mixed_blocks(paragraph_block, heading_blocks, list_blocks, todo_blocks,
                 code_block, quote_block, divider_block):
    """A realistic page with mixed block types."""
    return [
        *heading_blocks,
        paragraph_block,
        *list_blocks,
        *todo_blocks,
        quote_block,
        code_block,
        divider_block,
    ]


@pytest.fixture
def sample_page():
    """A minimal Notion page API response."""
    return {
        "id": "page-001",
        "object": "page",
        "url": "https://www.notion.so/Test-Page-page001",
        "properties": {
            "title": {
                "id": "title",
                "type": "title",
                "title": [{"plain_text": "Test Page"}],
            },
        },
    }


@pytest.fixture
def sample_database():
    """A minimal Notion database API response (2025-09-03 shape)."""
    return {
        "id": "db-001",
        "object": "database",
        "title": [{"plain_text": "Test DB"}],
        "url": "https://www.notion.so/db001",
        "data_sources": [{"id": "ds-001", "name": "Test DB"}],
    }


@pytest.fixture
def sample_data_source():
    """A data source response with properties."""
    return {
        "id": "ds-001",
        "object": "data_source",
        "title": [{"plain_text": "Test DB"}],
        "properties": {
            "Name": {"id": "title", "type": "title", "title": {}},
            "Status": {
                "id": "prop-status",
                "type": "select",
                "select": {
                    "options": [
                        {"name": "To Do", "color": "red"},
                        {"name": "Done", "color": "green"},
                    ]
                },
            },
            "Priority": {"id": "prop-pri", "type": "number", "number": {"format": "number"}},
        },
    }


# ---------------------------------------------------------------------------
# Integration test markers
# ---------------------------------------------------------------------------

# Integration tests need a sandbox page + database the token can write to.
SANDBOX_PAGE_ID = os.environ.get("NOTION_TEST_PAGE_ID", "")
SANDBOX_DB_ID = os.environ.get("NOTION_TEST_DB_ID", "")

requires_notion = pytest.mark.skipif(
    not (
        os.environ.get("NOTION_TEST_TOKEN")
        and SANDBOX_PAGE_ID
        and SANDBOX_DB_ID
    ),
    reason=(
        "Set NOTION_TEST_TOKEN + NOTION_TEST_PAGE_ID + NOTION_TEST_DB_ID "
        "to run integration tests"
    ),
)
