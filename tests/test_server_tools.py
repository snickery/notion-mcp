"""Unit tests for server.py tools that wrap the Notion client.

These exercise the MCP tool bodies (notion_query_database,
notion_batch_add_file_to_row, notion_purge_file, etc.) with the
underlying NotionClient methods patched out. The goal is to verify
parameter wiring, output formatting, and cursor/pagination plumbing —
not to re-test the client itself.
"""
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_shared_dir(tmp_path, monkeypatch):
    d = tmp_path / "shared"
    d.mkdir()
    monkeypatch.setattr("notion_mcp.config.SHARED_DIR", str(d))
    import notion_mcp.server as server
    monkeypatch.setattr(server, "SHARED_DIR", str(d))
    return d


# ===================================================================
# notion_query_database
# ===================================================================

def _row(page_id, title, props=None):
    full = {
        "Name": {"type": "title", "title": [{"plain_text": title}]},
    }
    if props:
        full.update(props)
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "properties": full,
    }


class TestNotionQueryDatabase:
    def test_returns_cursor_on_has_more(self):
        import notion_mcp.server as server
        envelope = {
            "results": [_row("p1", "Row 1", {
                "Status": {"type": "select", "select": {"name": "Done"}},
            })],
            "has_more": True,
            "next_cursor": "cur-next",
        }
        with patch.object(server.notion, "resolve_data_source_id", return_value="ds-1"), \
             patch.object(server.notion, "query_data_source_page", return_value=envelope):
            out = server.notion_query_database("db-1", limit=1)
        assert "Row 1" in out
        assert "has_more: True" in out
        assert "next_cursor: cur-next" in out
        assert "Pass next_cursor" in out

    def test_terminal_page_shows_end_marker(self):
        import notion_mcp.server as server
        envelope = {
            "results": [_row("p1", "Only")],
            "has_more": False,
            "next_cursor": None,
        }
        with patch.object(server.notion, "resolve_data_source_id", return_value="ds-1"), \
             patch.object(server.notion, "query_data_source_page", return_value=envelope):
            out = server.notion_query_database("db-1")
        assert "next_cursor: (end)" in out
        assert "Pass next_cursor" not in out

    def test_start_cursor_is_forwarded(self):
        import notion_mcp.server as server
        envelope = {"results": [], "has_more": False}
        with patch.object(server.notion, "resolve_data_source_id", return_value="ds-1"), \
             patch.object(server.notion, "query_data_source_page", return_value=envelope) as m:
            server.notion_query_database(
                "db-1", start_cursor="cur-resume", limit=25,
            )
            kwargs = m.call_args[1]
            assert kwargs["start_cursor"] == "cur-resume"
            assert kwargs["page_size"] == 25

    def test_properties_filter_slims_output(self):
        import notion_mcp.server as server
        envelope = {
            "results": [_row("p1", "Row", props={
                "Status": {"type": "select", "select": {"name": "Done"}},
                "Priority": {"type": "number", "number": 3},
                "Notes": {"type": "rich_text", "rich_text": [
                    {"plain_text": "secret long notes"},
                ]},
            })],
            "has_more": False,
        }
        with patch.object(server.notion, "resolve_data_source_id", return_value="ds-1"), \
             patch.object(server.notion, "query_data_source_page", return_value=envelope):
            out = server.notion_query_database(
                "db-1", properties="Status,Priority",
            )
        assert "Status" in out
        assert "Priority" in out
        # The Notes column should be excluded.
        assert "secret long notes" not in out

    def test_limit_clamped_to_notion_max(self):
        import notion_mcp.server as server
        envelope = {"results": [], "has_more": False}
        with patch.object(server.notion, "resolve_data_source_id", return_value="ds-1"), \
             patch.object(server.notion, "query_data_source_page", return_value=envelope) as m:
            server.notion_query_database("db-1", limit=500)
            assert m.call_args[1]["page_size"] == 100


# ===================================================================
# notion_batch_add_file_to_row
# ===================================================================

class TestNotionBatchAddFileToRow:
    def test_processes_each_item(self):
        import json

        import notion_mcp.server as server

        def fake_add(page_id, source, files_property, mode, filename):
            return {
                "page_id": page_id,
                "files_property": files_property,
                "mode": mode,
                "name": f"{page_id}.pdf",
                "size": 10,
                "mime": "application/pdf",
                "total_files": 1,
            }

        items = [
            {"page_id": "p1", "source": "shared:a.pdf"},
            {"page_id": "p2", "source": "shared:b.pdf"},
        ]
        with patch.object(server, "_add_file_to_row", side_effect=fake_add):
            out = server.notion_batch_add_file_to_row(
                json.dumps(items),
                default_files_property="Attachments",
            )
        assert "2 item(s)" in out
        assert "[0] OK page=p1" in out
        assert "[1] OK page=p2" in out
        assert "Summary: 2 ok, 0 failed" in out

    def test_partial_failure_does_not_abort(self):
        import json

        import notion_mcp.server as server

        def fake_add(page_id, source, *a, **kw):
            if page_id == "bad":
                raise ValueError("boom")
            return {
                "page_id": page_id, "files_property": "F",
                "mode": "append", "name": "x", "size": 1,
                "mime": "text/plain", "total_files": 1,
            }

        items = [
            {"page_id": "good", "source": "shared:a"},
            {"page_id": "bad", "source": "shared:b"},
            {"page_id": "also-good", "source": "shared:c"},
        ]
        with patch.object(server, "_add_file_to_row", side_effect=fake_add):
            out = server.notion_batch_add_file_to_row(
                json.dumps(items), default_files_property="F",
            )
        assert "OK page=good" in out
        assert "FAIL page=bad" in out
        assert "OK page=also-good" in out
        assert "Summary: 2 ok, 1 failed" in out

    def test_rejects_non_list(self):
        import notion_mcp.server as server
        with pytest.raises(ValueError, match="non-empty JSON array"):
            server.notion_batch_add_file_to_row('{"a": 1}')

    def test_rejects_invalid_json(self):
        import notion_mcp.server as server
        with pytest.raises(ValueError, match="not valid JSON"):
            server.notion_batch_add_file_to_row("not json")

    def test_missing_fields_are_reported_not_raised(self):
        import json

        import notion_mcp.server as server
        items = [{"page_id": "p1"}]  # missing source + files_property
        out = server.notion_batch_add_file_to_row(json.dumps(items))
        assert "SKIP" in out
        assert "missing" in out
        assert "Summary: 0 ok, 1 failed" in out


# ===================================================================
# notion_purge_file
# ===================================================================

class TestNotionPurgeFile:
    def test_deletes_target_file(self, tmp_shared_dir):
        import notion_mcp.server as server
        p = tmp_shared_dir / "a.pdf"
        p.write_bytes(b"bye")
        out = server.notion_purge_file("a.pdf")
        assert not p.exists()
        assert "Deleted a.pdf" in out

    def test_missing_file(self, tmp_shared_dir):
        import notion_mcp.server as server
        out = server.notion_purge_file("ghost.pdf")
        assert "No such file" in out

    def test_rejects_path_traversal(self, tmp_shared_dir):
        import notion_mcp.server as server
        with pytest.raises(ValueError, match="bare filename"):
            server.notion_purge_file("../secrets.txt")


# ===================================================================
# notion_download_file — destination='shared' (cross-MCP reverse path)
# ===================================================================

class TestNotionDownloadFileShared:
    def _patch_fetch(self, server, payload=b"%PDF-1.4 notion bytes"):
        return patch.object(
            server.notion, "fetch_file_block_bytes",
            return_value=("doc.pdf", "application/pdf", payload),
        )

    def test_writes_to_shared_mount(self, tmp_shared_dir):
        import hashlib

        import notion_mcp.server as server
        payload = b"%PDF-1.4 notion bytes"
        with self._patch_fetch(server, payload):
            out = server.notion_download_file("block-1", destination="shared")
        saved = tmp_shared_dir / "doc.pdf"
        assert saved.read_bytes() == payload
        # Output must hand the agent a ready-to-use gmail reference,
        # the sha256, and the TTL warning.
        assert "shared:doc.pdf" in out
        assert hashlib.sha256(payload).hexdigest() in out
        assert "24h" in out

    def test_collision_auto_renames(self, tmp_shared_dir):
        import notion_mcp.server as server
        (tmp_shared_dir / "doc.pdf").write_bytes(b"existing")
        with self._patch_fetch(server):
            out = server.notion_download_file("block-1", destination="shared")
        assert "doc-2.pdf" in out
        assert (tmp_shared_dir / "doc.pdf").read_bytes() == b"existing"
        assert (tmp_shared_dir / "doc-2.pdf").exists()

    def test_rejects_path_components(self, tmp_shared_dir):
        import notion_mcp.server as server
        with self._patch_fetch(server):
            with pytest.raises(ValueError, match="bare filename"):
                server.notion_download_file(
                    "block-1", destination="shared", filename="../escape.pdf",
                )

    def test_unknown_destination_lists_options(self, tmp_shared_dir):
        import notion_mcp.server as server
        with self._patch_fetch(server):
            with pytest.raises(ValueError, match="shared"):
                server.notion_download_file("block-1", destination="bogus")


# ===================================================================
# notion_list_local_files — sha256 in listing
# ===================================================================

class TestListLocalFilesSha:
    def test_shared_listing_includes_sha256(self, tmp_shared_dir):
        import hashlib

        import notion_mcp.server as server
        (tmp_shared_dir / "x.bin").write_bytes(b"abc")
        out = server.notion_list_local_files(location="shared")
        assert "x.bin" in out
        assert hashlib.sha256(b"abc").hexdigest() in out


# ===================================================================
# Position mapping + multi-part upload + new file tools
# ===================================================================

class TestPositionParam:
    def test_end_is_none(self):
        import notion_mcp.server as server
        assert server._position_param("end") is None
        assert server._position_param("") is None
        assert server._position_param("END") is None

    def test_start(self):
        import notion_mcp.server as server
        assert server._position_param("start") == {"type": "start"}

    def test_block_id_means_after(self):
        import notion_mcp.server as server
        assert server._position_param("abc-123") == {
            "type": "after_block", "after_block": {"id": "abc-123"},
        }


class TestMultiPartUpload:
    def test_small_payload_single_part(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "create_file_upload",
                          return_value={"id": "up-1"}) as create, \
             patch.object(server.notion, "send_file_upload") as send, \
             patch.object(server.notion, "complete_file_upload") as complete:
            out = server._upload_bytes_to_notion(b"x" * 1000, "a.pdf", "application/pdf")
        assert out == "up-1"
        create.assert_called_once_with("a.pdf", "application/pdf")
        send.assert_called_once()
        assert send.call_args.kwargs.get("part_number") is None
        complete.assert_not_called()

    def test_large_payload_multi_part(self):
        import notion_mcp.server as server
        payload = b"x" * (25 * 1024 * 1024)  # 25 MB → 3 parts of 10 MiB
        with patch.object(server.notion, "create_file_upload",
                          return_value={"id": "up-2"}) as create, \
             patch.object(server.notion, "send_file_upload") as send, \
             patch.object(server.notion, "complete_file_upload") as complete:
            out = server._upload_bytes_to_notion(payload, "big.pdf", "application/pdf")
        assert out == "up-2"
        assert create.call_args.kwargs["mode"] == "multi_part"
        assert create.call_args.kwargs["number_of_parts"] == 3
        assert send.call_count == 3
        part_numbers = [c.kwargs["part_number"] for c in send.call_args_list]
        assert part_numbers == [1, 2, 3]
        sent_total = sum(len(c.args[2]) for c in send.call_args_list)
        assert sent_total == len(payload)
        complete.assert_called_once_with("up-2")


class TestNotionReplaceFile:
    def test_replaces_in_place(self, tmp_shared_dir):
        import notion_mcp.server as server
        (tmp_shared_dir / "v2.pdf").write_bytes(b"%PDF-2")
        with patch.object(server.notion, "retrieve_block",
                          return_value={"type": "pdf"}), \
             patch.object(server, "_upload_bytes_to_notion",
                          return_value="up-9") as up, \
             patch.object(server.notion, "update_file_block") as update:
            out = server.notion_replace_file("blk-1", "shared:v2.pdf")
        up.assert_called_once()
        update.assert_called_once_with(
            "blk-1", "pdf", "up-9", caption=None, name="v2.pdf",
        )
        assert "in place" in out
        assert "blk-1" in out

    def test_type_mismatch_rejected(self, tmp_shared_dir):
        import notion_mcp.server as server
        (tmp_shared_dir / "v2.pdf").write_bytes(b"%PDF-2")
        with patch.object(server.notion, "retrieve_block",
                          return_value={"type": "image"}):
            with pytest.raises(ValueError, match="maps to 'pdf'"):
                server.notion_replace_file("blk-1", "shared:v2.pdf")

    def test_non_file_block_rejected(self, tmp_shared_dir):
        import notion_mcp.server as server
        (tmp_shared_dir / "v2.pdf").write_bytes(b"%PDF-2")
        with patch.object(server.notion, "retrieve_block",
                          return_value={"type": "paragraph"}):
            with pytest.raises(ValueError, match="not a file block"):
                server.notion_replace_file("blk-1", "shared:v2.pdf")


class TestImportFromUrl:
    def test_immediate_upload_attaches(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "create_file_upload",
                          return_value={"id": "up-7", "status": "uploaded"}), \
             patch.object(server.notion, "attach_file_to_page",
                          return_value={"results": [{"id": "blk-new"}]}) as attach:
            out = server.notion_import_file_from_url(
                "https://example.com/r.pdf", "page-1", "r.pdf",
                position="start",
            )
        assert "blk-new" in out
        assert attach.call_args.kwargs["position"] == {"type": "start"}
        assert attach.call_args.kwargs["block_type"] == "pdf"

    def test_failed_import_raises(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "create_file_upload",
                          return_value={"id": "up-8", "status": "failed"}), \
             patch.object(server.notion, "retrieve_file_upload",
                          return_value={"status": "failed",
                                        "file_import_result": "404 from origin"}):
            with pytest.raises(RuntimeError, match="failed to import"):
                server.notion_import_file_from_url(
                    "https://example.com/gone.pdf", "page-1", "gone.pdf",
                )


class TestSetPageVisual:
    def test_emoji_icon(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "_request") as req:
            out = server.notion_set_page_visual("page-1", "icon", emoji="📊")
        req.assert_called_once_with(
            "PATCH", "/pages/page-1",
            json={"icon": {"type": "emoji", "emoji": "📊"}},
        )
        assert "icon" in out

    def test_cover_requires_image(self, tmp_shared_dir):
        import notion_mcp.server as server
        (tmp_shared_dir / "doc.pdf").write_bytes(b"%PDF")
        with pytest.raises(ValueError, match="must be an image"):
            server.notion_set_page_visual("page-1", "cover", source="shared:doc.pdf")

    def test_cover_from_source(self, tmp_shared_dir):
        import notion_mcp.server as server
        (tmp_shared_dir / "art.png").write_bytes(b"\x89PNG fake")
        with patch.object(server, "_upload_bytes_to_notion",
                          return_value="up-5"), \
             patch.object(server.notion, "_request") as req:
            out = server.notion_set_page_visual("page-1", "cover", source="shared:art.png")
        req.assert_called_once_with(
            "PATCH", "/pages/page-1",
            json={"cover": {"type": "file_upload", "file_upload": {"id": "up-5"}}},
        )
        assert "cover" in out

    def test_rejects_both_or_neither(self):
        import notion_mcp.server as server
        with pytest.raises(ValueError, match="exactly one"):
            server.notion_set_page_visual("page-1", "icon")
        with pytest.raises(ValueError, match="exactly one"):
            server.notion_set_page_visual("page-1", "icon", source="shared:x.png", emoji="📊")

    def test_emoji_only_for_icon(self):
        import notion_mcp.server as server
        with pytest.raises(ValueError, match="only valid for target='icon'"):
            server.notion_set_page_visual("page-1", "cover", emoji="📊")

    def test_remove_cover(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "_request") as req:
            out = server.notion_set_page_visual("page-1", "cover", remove=True)
        req.assert_called_once_with("PATCH", "/pages/page-1", json={"cover": None})
        assert "Removed cover" in out

    def test_remove_rejects_source(self):
        import notion_mcp.server as server
        with pytest.raises(ValueError, match="neither source nor emoji"):
            server.notion_set_page_visual("page-1", "icon", emoji="x", remove=True)


# ===================================================================
# notion_batch_add_file_to_row — bounded concurrency (2026-06-14)
# ===================================================================

class TestBatchConcurrency:
    def test_runs_in_parallel_but_bounded_and_ordered(self, monkeypatch):
        import json
        import threading
        import time

        import notion_mcp.server as server
        monkeypatch.setattr(server, "_BATCH_CONCURRENCY", 3)
        cur = {"n": 0, "max": 0}
        lk = threading.Lock()

        def fake_add(page_id, source, files_property, mode, filename):
            with lk:
                cur["n"] += 1
                cur["max"] = max(cur["max"], cur["n"])
            time.sleep(0.05)
            with lk:
                cur["n"] -= 1
            return {"page_id": page_id, "files_property": files_property,
                    "mode": mode, "name": f"{page_id}.pdf", "size": 1,
                    "mime": "application/pdf", "total_files": 1}

        items = [{"page_id": f"p{i}", "source": f"shared:{i}.pdf"} for i in range(9)]
        with patch.object(server, "_add_file_to_row", side_effect=fake_add):
            out = server.notion_batch_add_file_to_row(
                json.dumps(items), default_files_property="F")
        assert cur["max"] <= 3            # never exceeded the bound
        assert cur["max"] >= 2            # genuinely ran in parallel
        assert "concurrency=3" in out
        assert "Summary: 9 ok, 0 failed" in out
        # output stays in input order despite out-of-order completion
        positions = [out.index(f"[{i}] OK") for i in range(9)]
        assert positions == sorted(positions)

    def test_same_page_never_overlaps(self, monkeypatch):
        import json
        import threading
        import time

        import notion_mcp.server as server
        monkeypatch.setattr(server, "_BATCH_CONCURRENCY", 4)
        state = {"active": 0, "overlapped": False}
        lk = threading.Lock()

        def fake_add(page_id, source, *a, **kw):
            with lk:
                state["active"] += 1
                if state["active"] > 1:
                    state["overlapped"] = True
            time.sleep(0.03)
            with lk:
                state["active"] -= 1
            return {"page_id": page_id, "files_property": "F", "mode": "append",
                    "name": "x", "size": 1, "mime": "text/plain", "total_files": 1}

        # all four items target the SAME row -> per-page lock must serialise
        items = [{"page_id": "samepage", "source": f"shared:{i}.pdf"} for i in range(4)]
        with patch.object(server, "_add_file_to_row", side_effect=fake_add):
            out = server.notion_batch_add_file_to_row(
                json.dumps(items), default_files_property="F")
        assert state["overlapped"] is False   # per-page lock held
        assert "Summary: 4 ok, 0 failed" in out

    def test_large_batch_soft_warn(self, monkeypatch):
        import json

        import notion_mcp.server as server
        monkeypatch.setattr(server, "_BATCH_CONCURRENCY", 3)

        def fake_add(page_id, source, files_property, mode, filename):
            return {"page_id": page_id, "files_property": files_property,
                    "mode": mode, "name": "x", "size": 1, "mime": "x",
                    "total_files": 1}

        items = [{"page_id": f"p{i}", "source": f"shared:{i}"} for i in range(40)]
        with patch.object(server, "_add_file_to_row", side_effect=fake_add):
            out = server.notion_batch_add_file_to_row(
                json.dumps(items), default_files_property="F")
        assert "⚠ Large batch" in out
        assert "Summary: 40 ok, 0 failed" in out


# ===================================================================
# 2026-10 gap closure
# ===================================================================

class TestIconParam:
    def test_forms(self):
        import notion_mcp.server as server
        assert server._icon_param("remove") is None
        assert server._icon_param("🚀") == {"type": "emoji", "emoji": "🚀"}
        assert server._icon_param("icon:pizza:blue") == {
            "type": "icon", "icon": {"name": "pizza", "color": "blue"},
        }
        assert server._icon_param("icon:pizza") == {
            "type": "icon", "icon": {"name": "pizza"},
        }
        assert server._icon_param("https://x.test/i.png") == {
            "type": "external", "external": {"url": "https://x.test/i.png"},
        }

    def test_custom_emoji_by_id_skips_lookup(self):
        import notion_mcp.server as server
        uid = "43833259-72ae-404e-8441-b6577f3159b4"
        with patch.object(server.notion, "list_custom_emojis") as m:
            assert server._icon_param(f"custom_emoji:{uid}") == {
                "type": "custom_emoji", "custom_emoji": {"id": uid},
            }
            m.assert_not_called()

    def test_custom_emoji_by_name_is_resolved(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "list_custom_emojis",
                          return_value=[{"id": "e-1", "name": "party"}]) as m:
            assert server._icon_param("custom_emoji::party:") == {
                "type": "custom_emoji", "custom_emoji": {"id": "e-1"},
            }
            m.assert_called_once_with(name="party")

    def test_unknown_custom_emoji_is_an_error(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "list_custom_emojis", return_value=[]), \
             pytest.raises(ValueError, match="No custom emoji"):
            server._icon_param("custom_emoji:nope")


class TestCreatePageOptions:
    def _create(self, **kw):
        import notion_mcp.server as server
        with patch.object(server.notion, "create_page",
                          return_value={"id": "p1", "url": "u"}) as m:
            out = server.notion_create_page("T", parent_page_id="pp", **kw)
        return out, (m.call_args[1] if m.called else None)

    def test_default_converts_markdown_to_blocks(self):
        _, kw = self._create(content="hello")
        assert kw["children"] and kw["markdown"] == ""
        assert kw["silent"] is False and kw["template"] is None

    def test_native_markdown_sends_text(self):
        _, kw = self._create(content="hello", native_markdown=True, markdown_version="v2")
        assert kw["children"] is None
        assert kw["markdown"] == "hello" and kw["markdown_version"] == "v2"

    def test_template_default_and_id(self):
        _, kw = self._create(template="default", silent=True)
        assert kw["template"] == {"type": "default"} and kw["silent"] is True
        _, kw = self._create(template="tpl-1")
        assert kw["template"] == {"type": "template_id", "template_id": "tpl-1"}

    def test_template_with_content_is_refused(self):
        out, kw = self._create(template="default", content="x")
        assert kw is None and "template" in out

    def test_position_forms(self):
        assert self._create(position="start")[1]["position"] == {"type": "page_start"}
        assert self._create(position="end")[1]["position"] == {"type": "page_end"}
        assert self._create(position="blk")[1]["position"] == {
            "type": "after_block", "after_block": {"id": "blk"},
        }

    def test_pending_async_task_is_reported(self):
        import notion_mcp.server as server
        task = {"object": "async_task", "id": "t9", "status": "running"}
        with patch.object(server.notion, "create_page", return_value=task):
            out = server.notion_create_page(
                "T", parent_page_id="pp", content="x", native_markdown=True,
            )
        assert "t9" in out and "notion_get_async_task" in out


class TestWriteToolWiring:
    def test_update_page_silent_and_icon(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "update_page") as m:
            server.notion_update_page("p1", icon="remove", silent=True)
        assert m.call_args[0] == ("p1", {"icon": None})
        assert m.call_args[1] == {"silent": True}

    def test_append_content_position_and_silent(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "append_blocks", return_value=[{}]) as m:
            server.notion_append_content("p1", "hi", position="start", silent=True)
        assert m.call_args[1] == {"position": {"type": "start"}, "silent": True}

    def test_insert_page_content(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "insert_page_markdown") as m:
            server.notion_insert_page_content("p1", "x", position="start")
            assert m.call_args[1] == {"position": "start", "after": ""}
            server.notion_insert_page_content("p1", "x", after="a...b")
            assert m.call_args[1] == {"position": "", "after": "a...b"}
            m.reset_mock()
            assert "position must be" in server.notion_insert_page_content(
                "p1", "x", position="middle",
            )
            m.assert_not_called()

    def test_replace_reports_pending_task(self):
        import notion_mcp.server as server
        task = {"object": "async_task", "id": "t1", "status": "queued"}
        with patch.object(server.notion, "replace_page_markdown", return_value=task):
            assert "t1" in server.notion_replace_page_content("p1", "x")
        with patch.object(server.notion, "replace_page_markdown",
                          return_value={"object": "page_markdown"}):
            assert server.notion_replace_page_content("p1", "x").startswith("Replaced")

    def test_get_async_task_states(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "get_async_task", return_value={
            "status": "succeeded", "result": {"object": "page", "id": "p1"},
        }):
            assert "page p1" in server.notion_get_async_task("t1")
        with patch.object(server.notion, "get_async_task", return_value={
            "status": "failed", "error": {"code": "c", "message": "m"},
        }):
            assert "c: m" in server.notion_get_async_task("t1")
        with patch.object(server.notion, "get_async_task", return_value={
            "status": "running", "poll_after_seconds": 3,
        }):
            assert "3s" in server.notion_get_async_task("t1")

    def test_upload_embed_html(self, tmp_shared_dir):
        import notion_mcp.server as server
        (tmp_shared_dir / "w.html").write_text("<h1>x</h1>")
        (tmp_shared_dir / "w.txt").write_text("x")
        with patch.object(server, "_upload_bytes_to_notion", return_value="u1") as up, \
             patch.object(server.notion, "attach_html_embed",
                          return_value={"results": [{"id": "b1"}]}) as emb, \
             patch.object(server.notion, "attach_file_to_page") as plain:
            out = server.notion_upload_file("shared:w.html", "p1", embed_html=True)
            assert up.call_args[0][2] == "text/html"
            emb.assert_called_once()
            plain.assert_not_called()
            assert "Block type: embed" in out
            with pytest.raises(ValueError, match="html"):
                server.notion_upload_file("shared:w.txt", "p1", embed_html=True)


class TestAddTabs:
    def test_labels_then_content(self):
        import notion_mcp.server as server
        tabs = '[{"label": "A", "icon": "📋", "content": "hello"}, {"label": "B"}]'
        with patch.object(server.notion, "append_blocks",
                          return_value=[{"id": "tab-1"}]) as app, \
             patch.object(server.notion, "list_children",
                          return_value=[{"id": "item-a"}, {"id": "item-b"}]):
            out = server.notion_add_tabs("p1", tabs)
        first = app.call_args_list[0]
        items = first[0][1][0]["tab"]["children"]
        assert [i["paragraph"].get("icon") for i in items] == [{"emoji": "📋"}, None]
        # only tab A has content, appended under its own item
        assert len(app.call_args_list) == 2
        assert app.call_args_list[1][0][0] == "item-a"
        assert "tab-1" in out

    def test_rejects_bad_json_shape(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "append_blocks") as app:
            assert "tabs_json must be" in server.notion_add_tabs("p1", '[{"x": 1}]')
            app.assert_not_called()


class TestDatabaseTools:
    def test_create_requires_exactly_one_of_schema_or_type(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "create_database") as m:
            assert "exactly one" in server.notion_create_database("pp", "T")
            assert "exactly one" in server.notion_create_database(
                "pp", "T", schema_json='{"N": {"title": {}}}', database_type="tasks",
            )
            m.assert_not_called()

    def test_create_typed(self):
        import notion_mcp.server as server
        db = {"id": "d1", "title": [{"plain_text": "Tasks"}]}
        with patch.object(server.notion, "create_database", return_value=db) as m:
            out = server.notion_create_database("pp", database_type="tasks", inline=True)
        assert m.call_args[1] == {
            "properties": None, "database_type": "tasks", "is_inline": True,
        }
        assert "Title: Tasks" in out

    def test_query_archived_and_limit_warning(self):
        import notion_mcp.server as server
        envelope = {
            "results": [_row("p1", "Row 1")], "has_more": False,
            "request_status": {
                "type": "incomplete",
                "incomplete_reason": "query_result_limit_reached",
            },
        }
        with patch.object(server.notion, "resolve_data_source_id", return_value="ds"), \
             patch.object(server.notion, "query_data_source_page",
                          return_value=envelope) as m:
            out = server.notion_query_database("db", archived=True)
        assert m.call_args[1]["is_archived"] is True
        assert "10,000-result limit" in out

    def test_list_templates(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "resolve_data_source_id", return_value="ds"), \
             patch.object(server.notion, "list_templates", return_value=[
                 {"id": "t1", "name": "Weekly", "is_default": True},
             ]):
            out = server.notion_list_templates("db")
        assert "Weekly (default)" in out and "t1" in out


class TestMeetingNotesTools:
    BLOCK = {
        "id": "mn-1", "type": "meeting_notes", "created_time": "2026-10-01T00:00:00Z",
        "meeting_notes": {
            "title": [{"plain_text": "Sync"}], "status": "notes_ready",
            "children": {"summary_block_id": "s-1", "notes_block_id": "n-1"},
            "calendar_event": {"attendees": ["u1", "u2"],
                               "start_time": "2026-10-01T10:00:00Z"},
        },
    }

    def test_query_defaults_and_title_filter(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "query_meeting_notes", return_value={
            "results": [self.BLOCK], "has_more": True,
        }) as m:
            out = server.notion_query_meeting_notes(title_contains="Sync")
        assert m.call_args[1]["filter"] == {
            "property": "title", "filter": {"string_contains": "Sync"},
        }
        assert m.call_args[1]["sort"][0]["direction"] == "descending"
        assert "Sync [notes_ready]" in out and "attendees: 2" in out
        assert "More exist" in out

    def test_read_section(self):
        import notion_mcp.server as server
        para = {"id": "x", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "plain_text": "Decided X",
                                             "text": {"content": "Decided X"}}]}}
        with patch.object(server.notion, "retrieve_block", return_value=self.BLOCK), \
             patch.object(server.notion, "get_block_children_deep",
                          return_value=[para]) as deep:
            out = server.notion_read_meeting_notes("mn-1")
            deep.assert_called_once_with("s-1")
            assert "Decided X" in out
            # transcript id is absent on this block
            assert "no transcript yet" in server.notion_read_meeting_notes(
                "mn-1", section="transcript",
            )

    def test_read_rejects_other_blocks_and_sections(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "retrieve_block",
                          return_value={"id": "b", "type": "paragraph"}):
            assert "not meeting notes" in server.notion_read_meeting_notes("b")
        assert "section must be" in server.notion_read_meeting_notes("b", section="x")

    def test_create_from_existing_block(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "create_meeting_notes",
                          return_value={"id": "mn-2"}) as m, \
             patch.object(server, "_upload_bytes_to_notion") as up:
            out = server.notion_create_meeting_notes("notion:blk-1", summarize=False)
        up.assert_not_called()
        assert m.call_args[0][0] == {"type": "block", "block_id": "blk-1"}
        assert m.call_args[1]["kickoff_summary"] is False
        assert "mn-2" in out

    def test_create_from_file_needs_parent_and_fixes_m4a_mime(self, tmp_shared_dir):
        import notion_mcp.server as server
        (tmp_shared_dir / "call.m4a").write_bytes(b"\x00\x01")
        with patch.object(server.notion, "create_meeting_notes",
                          return_value={"id": "mn-3"}) as m, \
             patch.object(server, "_upload_bytes_to_notion", return_value="u1") as up:
            assert "parent_page_id is required" in server.notion_create_meeting_notes(
                "shared:call.m4a",
            )
            m.assert_not_called()
            server.notion_create_meeting_notes("shared:call.m4a", parent_page_id="p1")
        assert up.call_args[0][2] == "audio/mp4"
        assert m.call_args[0][0] == {"type": "file_upload", "file_upload_id": "u1"}
        assert m.call_args[1]["parent_page_id"] == "p1"


class TestSmallReadTools:
    def test_get_user_me_shows_workspace(self):
        import notion_mcp.server as server
        bot = {"id": "b1", "name": "Bot", "type": "bot", "bot": {
            "owner": {"type": "workspace"}, "workspace_name": "Home",
            "workspace_limits": {"max_file_upload_size_in_bytes": 5},
        }}
        with patch.object(server.notion, "get_user", return_value=bot) as m:
            out = server.notion_get_user("me")
        m.assert_called_once_with("me")
        assert "workspace: Home" in out and "max file upload: 5 bytes" in out

    def test_get_comment(self):
        import notion_mcp.server as server
        c = {"id": "c1", "rich_text": [{"plain_text": "hi"}],
             "created_by": {"id": "u1"}, "discussion_id": "d1",
             "parent": {"type": "page_id", "page_id": "p1"}}
        with patch.object(server.notion, "retrieve_comment", return_value=c):
            out = server.notion_get_comment("c1")
        assert "hi" in out and "discussion_id: d1" in out and "parent: p1" in out

    def test_list_custom_emojis_and_uploads(self):
        import notion_mcp.server as server
        with patch.object(server.notion, "list_custom_emojis",
                          return_value=[{"id": "e1", "name": "party"}]):
            assert ":party:" in server.notion_list_custom_emojis()
        with patch.object(server.notion, "list_file_uploads", return_value={
            "results": [{"id": "u1", "filename": "a.pdf", "status": "uploaded",
                         "content_type": "application/pdf", "content_length": 9}],
            "has_more": False,
        }):
            assert "a.pdf [uploaded]" in server.notion_list_file_uploads()

    def test_list_users_name_filter(self):
        import notion_mcp.server as server
        users = [{"id": "1", "name": "Nick", "type": "person"},
                 {"id": "2", "name": "Bot", "type": "bot"}]
        with patch.object(server.notion, "list_users", return_value=users):
            out = server.notion_list_users(name_contains="nic")
        assert "Nick" in out and "Bot" not in out
