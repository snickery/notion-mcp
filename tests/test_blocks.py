"""Unit tests for blocks.py — markdown <-> Notion block conversion.

Pure unit tests, no API calls or mocking needed.
"""

import pytest

from notion_mcp.blocks import (
    blocks_to_markdown,
    format_page_properties,
    format_property_value,
    markdown_to_blocks,
    md_to_rich_text,
    rich_text_to_md,
)
from tests.conftest import _rt, make_block


# ===================================================================
# rich_text_to_md
# ===================================================================

class TestRichTextToMd:
    def test_plain_text(self):
        assert rich_text_to_md([_rt("hello")]) == "hello"

    def test_bold(self):
        assert rich_text_to_md([_rt("bold", bold=True)]) == "**bold**"

    def test_italic(self):
        assert rich_text_to_md([_rt("em", italic=True)]) == "*em*"

    def test_bold_italic(self):
        assert rich_text_to_md([_rt("both", bold=True, italic=True)]) == "***both***"

    def test_code(self):
        assert rich_text_to_md([_rt("x=1", code=True)]) == "`x=1`"

    def test_strikethrough(self):
        assert rich_text_to_md([_rt("old", strikethrough=True)]) == "~~old~~"

    def test_link(self):
        result = rich_text_to_md([_rt("click", href="https://example.com")])
        assert result == "[click](https://example.com)"

    def test_code_not_linked(self):
        """Code annotations should not get wrapped in link syntax."""
        result = rich_text_to_md([_rt("code", code=True, href="https://x.com")])
        assert result == "`code`"

    def test_combined_segments(self):
        result = rich_text_to_md([_rt("start "), _rt("bold", bold=True), _rt(" end")])
        assert result == "start **bold** end"

    def test_empty_array(self):
        assert rich_text_to_md([]) == ""

    def test_empty_text_skipped(self):
        assert rich_text_to_md([_rt("")]) == ""


# ===================================================================
# md_to_rich_text
# ===================================================================

class TestMdToRichText:
    def test_plain(self):
        result = md_to_rich_text("hello")
        assert len(result) == 1
        assert result[0]["text"]["content"] == "hello"

    def test_bold(self):
        result = md_to_rich_text("**bold**")
        assert result[0]["text"]["content"] == "bold"
        assert result[0]["annotations"]["bold"] is True

    def test_italic(self):
        result = md_to_rich_text("*italic*")
        assert result[0]["text"]["content"] == "italic"
        assert result[0]["annotations"]["italic"] is True

    def test_bold_italic(self):
        result = md_to_rich_text("***both***")
        assert result[0]["annotations"]["bold"] is True
        assert result[0]["annotations"]["italic"] is True

    def test_inline_code(self):
        result = md_to_rich_text("`code`")
        assert result[0]["text"]["content"] == "code"
        assert result[0]["annotations"]["code"] is True

    def test_strikethrough(self):
        result = md_to_rich_text("~~gone~~")
        assert result[0]["text"]["content"] == "gone"
        assert result[0]["annotations"]["strikethrough"] is True

    def test_link(self):
        result = md_to_rich_text("[text](https://example.com)")
        assert result[0]["text"]["content"] == "text"
        assert result[0]["text"]["link"]["url"] == "https://example.com"

    def test_mixed_inline(self):
        result = md_to_rich_text("plain **bold** plain")
        assert len(result) == 3
        assert result[0]["text"]["content"] == "plain "
        assert result[1]["annotations"]["bold"] is True
        assert result[2]["text"]["content"] == " plain"

    def test_empty_string(self):
        assert md_to_rich_text("") == []


# ===================================================================
# blocks_to_markdown
# ===================================================================

class TestBlocksToMarkdown:
    def test_paragraph(self, paragraph_block):
        result = blocks_to_markdown([paragraph_block])
        assert "Hello world" in result

    def test_headings(self, heading_blocks):
        result = blocks_to_markdown(heading_blocks)
        assert "# H1 Title" in result
        assert "## H2 Title" in result
        assert "### H3 Title" in result

    def test_bullet_list(self, list_blocks):
        result = blocks_to_markdown(list_blocks[:2])
        assert "- Bullet A" in result
        assert "- Bullet B" in result

    def test_numbered_list(self, list_blocks):
        result = blocks_to_markdown(list_blocks[2:4])
        assert "1. First" in result
        assert "2. Second" in result

    def test_numbered_list_resets_after_non_numbered(self):
        blocks = [
            make_block("numbered_list_item", [_rt("One")]),
            make_block("paragraph", [_rt("break")]),
            make_block("numbered_list_item", [_rt("One again")]),
        ]
        result = blocks_to_markdown(blocks)
        lines = [l for l in result.split("\n") if l.strip()]
        assert lines[0] == "1. One"
        assert lines[1] == "break"
        assert lines[2] == "1. One again"

    def test_todo(self, todo_blocks):
        result = blocks_to_markdown(todo_blocks)
        assert "- [x] Done task" in result
        assert "- [ ] Open task" in result

    def test_quote(self, quote_block):
        result = blocks_to_markdown([quote_block])
        assert "> Wise words" in result

    def test_code_block(self, code_block):
        result = blocks_to_markdown([code_block])
        assert "```python" in result
        assert 'print("hello")' in result
        assert "```" in result

    def test_divider(self, divider_block):
        result = blocks_to_markdown([divider_block])
        assert "---" in result

    def test_include_block_ids(self, paragraph_block):
        result = blocks_to_markdown([paragraph_block], include_ids=True)
        assert "<!-- block:p-001 -->" in result

    def test_no_block_ids_by_default(self, paragraph_block):
        result = blocks_to_markdown([paragraph_block])
        assert "<!-- block:" not in result

    def test_nested_children(self):
        parent = make_block("bulleted_list_item", [_rt("Parent")], block_id="parent")
        child = make_block("bulleted_list_item", [_rt("Child")], block_id="child")
        parent["has_children"] = True
        parent["_children"] = [child]
        result = blocks_to_markdown([parent])
        assert "- Parent" in result
        assert "  - Child" in result

    def test_callout_with_emoji(self):
        block = make_block(
            "callout", [_rt("Note text")], block_id="co-001",
            icon={"type": "emoji", "emoji": "💡"},
        )
        result = blocks_to_markdown([block])
        assert "💡 Note text" in result

    def test_bookmark(self):
        block = make_block(
            "bookmark", block_id="bk-001",
            url="https://example.com",
            caption=[_rt("Example")],
        )
        # bookmark has no rich_text, just url and caption
        result = blocks_to_markdown([block])
        assert "[Example](https://example.com)" in result

    def test_child_page(self):
        block = make_block("child_page", block_id="cp-001", title="Sub Page")
        result = blocks_to_markdown([block])
        assert "[Page: Sub Page]" in result
        assert "cp-001" in result

    def test_child_database(self):
        block = make_block("child_database", block_id="cd-001", title="My DB")
        result = blocks_to_markdown([block])
        assert "[Database: My DB]" in result

    def test_image_block(self):
        block = make_block(
            "image", block_id="img-001",
            type="external",
            external={"url": "https://img.example.com/pic.png"},
            caption=[_rt("A photo")],
        )
        result = blocks_to_markdown([block])
        assert "![A photo](https://img.example.com/pic.png)" in result

    def test_table(self):
        rows = [
            {"table_row": {"cells": [[_rt("A")], [_rt("B")]]}},
            {"table_row": {"cells": [[_rt("1")], [_rt("2")]]}},
        ]
        block = {"id": "tbl-001", "type": "table", "table": {}, "has_children": True, "_children": rows}
        result = blocks_to_markdown([block])
        assert "| A | B |" in result
        assert "| --- | --- |" in result
        assert "| 1 | 2 |" in result

    def test_unknown_block_type(self):
        block = {"id": "unk-001", "type": "new_fancy_block", "has_children": False,
                 "new_fancy_block": {"rich_text": [_rt("mystery")]}}
        result = blocks_to_markdown([block])
        assert "mystery" in result
        assert "[new_fancy_block]" in result

    def test_empty_blocks(self):
        assert blocks_to_markdown([]) == ""


# ===================================================================
# markdown_to_blocks
# ===================================================================

class TestMarkdownToBlocks:
    def test_paragraph(self):
        blocks = markdown_to_blocks("Hello world")
        assert len(blocks) == 1
        assert blocks[0]["type"] == "paragraph"

    def test_headings(self):
        blocks = markdown_to_blocks("# H1\n## H2\n### H3")
        assert blocks[0]["type"] == "heading_1"
        assert blocks[1]["type"] == "heading_2"
        assert blocks[2]["type"] == "heading_3"

    def test_bullet_list(self):
        blocks = markdown_to_blocks("- A\n- B\n* C")
        assert all(b["type"] == "bulleted_list_item" for b in blocks)
        assert len(blocks) == 3

    def test_numbered_list(self):
        blocks = markdown_to_blocks("1. First\n2. Second")
        assert all(b["type"] == "numbered_list_item" for b in blocks)

    def test_todo_checked(self):
        blocks = markdown_to_blocks("- [x] Done")
        assert blocks[0]["type"] == "to_do"
        assert blocks[0]["to_do"]["checked"] is True

    def test_todo_unchecked(self):
        blocks = markdown_to_blocks("- [ ] Open")
        assert blocks[0]["type"] == "to_do"
        assert blocks[0]["to_do"]["checked"] is False

    def test_quote(self):
        blocks = markdown_to_blocks("> Line one\n> Line two")
        assert len(blocks) == 1
        assert blocks[0]["type"] == "quote"

    def test_code_block(self):
        blocks = markdown_to_blocks("```python\nx = 1\n```")
        assert blocks[0]["type"] == "code"
        assert blocks[0]["code"]["language"] == "python"
        assert blocks[0]["code"]["rich_text"][0]["text"]["content"] == "x = 1"

    def test_code_block_default_language(self):
        blocks = markdown_to_blocks("```\ncode\n```")
        assert blocks[0]["code"]["language"] == "plain text"

    def test_divider(self):
        for marker in ("---", "***", "___"):
            blocks = markdown_to_blocks(marker)
            assert blocks[0]["type"] == "divider"

    def test_empty_lines_skipped(self):
        blocks = markdown_to_blocks("\n\nHello\n\n")
        assert len(blocks) == 1

    def test_mixed_content(self):
        md = "# Title\n\nParagraph text.\n\n- Bullet\n\n1. Numbered\n\n---"
        blocks = markdown_to_blocks(md)
        types = [b["type"] for b in blocks]
        assert types == ["heading_1", "paragraph", "bulleted_list_item",
                         "numbered_list_item", "divider"]

    def test_inline_formatting_preserved(self):
        blocks = markdown_to_blocks("Text with **bold** and *italic*")
        rt = blocks[0]["paragraph"]["rich_text"]
        assert len(rt) == 4  # "Text with ", "bold", " and ", "italic"
        assert rt[1]["annotations"]["bold"] is True
        assert rt[3]["annotations"]["italic"] is True

    def test_todo_before_bullet(self):
        """To-do should be matched before generic bullet."""
        blocks = markdown_to_blocks("- [x] Task\n- Not a task")
        assert blocks[0]["type"] == "to_do"
        assert blocks[1]["type"] == "bulleted_list_item"


# ===================================================================
# format_property_value
# ===================================================================

class TestFormatPropertyValue:
    def test_title(self):
        assert format_property_value({
            "type": "title", "title": [{"plain_text": "Hello"}]
        }) == "Hello"

    def test_rich_text(self):
        assert format_property_value({
            "type": "rich_text", "rich_text": [{"plain_text": "Content"}]
        }) == "Content"

    def test_number(self):
        assert format_property_value({"type": "number", "number": 42}) == "42"

    def test_number_none(self):
        assert format_property_value({"type": "number", "number": None}) == ""

    def test_select(self):
        assert format_property_value({
            "type": "select", "select": {"name": "Active"}
        }) == "Active"

    def test_select_empty(self):
        assert format_property_value({"type": "select", "select": None}) == ""

    def test_multi_select(self):
        assert format_property_value({
            "type": "multi_select",
            "multi_select": [{"name": "A"}, {"name": "B"}],
        }) == "A, B"

    def test_date_single(self):
        assert format_property_value({
            "type": "date", "date": {"start": "2026-01-15"}
        }) == "2026-01-15"

    def test_date_range(self):
        assert format_property_value({
            "type": "date", "date": {"start": "2026-01-15", "end": "2026-01-20"}
        }) == "2026-01-15 -> 2026-01-20"

    def test_date_none(self):
        assert format_property_value({"type": "date", "date": None}) == ""

    def test_checkbox_true(self):
        assert format_property_value({"type": "checkbox", "checkbox": True}) == "Yes"

    def test_checkbox_false(self):
        assert format_property_value({"type": "checkbox", "checkbox": False}) == "No"

    def test_url(self):
        assert format_property_value({
            "type": "url", "url": "https://example.com"
        }) == "https://example.com"

    def test_email(self):
        assert format_property_value({
            "type": "email", "email": "a@b.com"
        }) == "a@b.com"

    def test_people(self):
        assert format_property_value({
            "type": "people",
            "people": [{"name": "Alice"}, {"name": "Bob"}],
        }) == "Alice, Bob"

    def test_files(self):
        assert format_property_value({
            "type": "files",
            "files": [{"name": "doc.pdf"}, {"name": "img.png"}],
        }) == "doc.pdf, img.png"

    def test_status(self):
        assert format_property_value({
            "type": "status", "status": {"name": "In Progress"}
        }) == "In Progress"

    def test_unique_id_with_prefix(self):
        assert format_property_value({
            "type": "unique_id", "unique_id": {"prefix": "PRJ", "number": 42}
        }) == "PRJ-42"

    def test_unique_id_no_prefix(self):
        assert format_property_value({
            "type": "unique_id", "unique_id": {"prefix": "", "number": 7}
        }) == "7"

    def test_created_time(self):
        assert format_property_value({
            "type": "created_time", "created_time": "2026-01-15T10:00:00Z"
        }) == "2026-01-15T10:00:00Z"

    def test_relation(self):
        result = format_property_value({
            "type": "relation", "relation": [{"id": "aaa"}, {"id": "bbb"}]
        })
        assert "aaa" in result and "bbb" in result

    def test_unknown_type(self):
        result = format_property_value({"type": "future_type", "future_type": "val"})
        assert result == "val"


class TestFormatPageProperties:
    def test_formats_non_empty_values(self):
        props = {
            "Name": {"type": "title", "title": [{"plain_text": "Test"}]},
            "Status": {"type": "select", "select": {"name": "Active"}},
            "Empty": {"type": "rich_text", "rich_text": []},
        }
        result = format_page_properties(props)
        assert "Name: Test" in result
        assert "Status: Active" in result
        assert "Empty" not in result  # empty values are excluded


# ===================================================================
# heading_4 (API addition 2026-03-30)
# ===================================================================


class TestHeading4:
    def test_md_to_blocks(self):
        blocks = markdown_to_blocks("#### Sub-sub-section")
        assert blocks[0]["type"] == "heading_4"

    def test_deeper_levels_clamp(self):
        blocks = markdown_to_blocks("###### very deep")
        assert blocks[0]["type"] == "heading_4"

    def test_blocks_to_md(self):
        block = {
            "id": "h4-1", "type": "heading_4",
            "heading_4": {"rich_text": [{"plain_text": "Deep", "annotations": {}}]},
        }
        assert blocks_to_markdown([block]).startswith("#### Deep")
