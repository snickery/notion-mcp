"""Unit tests for the PDF reading tools (notion_extract_file_text,
notion_render_file_page) and the 'notion:' source scheme.

The pdf_read module itself (OCR tiers, LLM tier, ceiling walk, renderer)
is duplicated verbatim from gmail-mcp and exhaustively tested THERE
(gmail-mcp tests/test_pdf_read.py) — these tests cover the notion-mcp
integration points only: scheme routing, tool wiring, and return types.

Fixtures (tests/fixtures/, regenerate with make_ocr_fixtures.py):
  scanned.pdf — 2 image-only pages; sample.pdf — 2 native-text pages.
OCR is delegated to xberg and stubbed at the client boundary here.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp_server_kit import pdf as pdf_read

import notion_mcp.server as server

FIXTURES = Path(__file__).parent / "fixtures"
SCANNED = (FIXTURES / "scanned.pdf").read_bytes()
SAMPLE = (FIXTURES / "sample.pdf").read_bytes()


@pytest.fixture
def tmp_files_dir(tmp_path, monkeypatch):
    d = tmp_path / "files"
    d.mkdir()
    monkeypatch.setattr("notion_mcp.config.FILES_DIR", str(d))
    monkeypatch.setattr(server, "FILES_DIR", str(d))
    return d


# ===================================================================
# 'notion:' source scheme
# ===================================================================


class TestNotionSourceScheme:
    def test_notion_scheme_fetches_block_bytes(self):
        with patch.object(
            server.notion, "fetch_file_block_bytes",
            return_value=("doc.pdf", "application/pdf", b"%PDF-1.4 x"),
        ) as fetch:
            data, name, mime = server._read_source("notion:blk-123")
        fetch.assert_called_once_with("blk-123")
        assert data == b"%PDF-1.4 x"
        assert name == "doc.pdf"
        assert mime == "application/pdf"

    def test_notion_scheme_filename_override(self):
        with patch.object(
            server.notion, "fetch_file_block_bytes",
            return_value=("doc.pdf", "application/pdf", b"%PDF-1.4 x"),
        ):
            _, name, _ = server._read_source(
                "notion: blk-123 ", filename="renamed.pdf",
            )
        assert name == "renamed.pdf"

    def test_unknown_scheme_error_mentions_notion(self):
        with pytest.raises(ValueError, match="notion"):
            server._read_source("nope")


# ===================================================================
# notion_extract_file_text
# ===================================================================


class TestExtractFileText:
    def test_scanned_notion_block_default_auto_ocr(self, monkeypatch):
        # OCR is delegated to xberg — stub the client boundary; the request
        # shape itself is covered by gmail-mcp's authoritative suite.
        monkeypatch.setattr(
            pdf_read, "xberg_ocr_pages",
            lambda fb, pages, *, vlm=False, concurrency=None: (
                {p: f"Total due 421.50 (page {p})" for p in pages},
                "xberg:tesseract",
            ),
        )
        with patch.object(
            server.notion, "fetch_file_block_bytes",
            return_value=("scan.pdf", "application/pdf", SCANNED),
        ):
            out = server.notion_extract_file_text("notion:blk-1")
        data = json.loads(out)
        assert data["ocr_used"] is True
        assert data["ocr_pages"] == [1, 2]
        assert data["ocr_engine"] == "xberg:tesseract"
        assert "421.50" in data["text"]
        assert data["filename"] == "scan.pdf"
        assert data["source"] == "notion:blk-1"

    def test_local_source_ocr_off(self, tmp_files_dir):
        (tmp_files_dir / "scan.pdf").write_bytes(SCANNED)
        out = server.notion_extract_file_text("local:scan.pdf", ocr="off")
        data = json.loads(out)
        assert "ocr_used" not in data
        assert data["text"].strip() == ""
        assert data["page_count"] == 2

    def test_text_pdf_pages_subset(self, tmp_files_dir):
        (tmp_files_dir / "sample.pdf").write_bytes(SAMPLE)
        out = server.notion_extract_file_text("local:sample.pdf", pages="2")
        data = json.loads(out)
        assert data["pages_returned"] == [2]
        assert "Reference: INV-00042" in data["text"]
        assert "Invoice total" not in data["text"]

    def test_bad_ocr_rejected_before_fetch(self):
        with patch.object(
            server.notion, "fetch_file_block_bytes",
        ) as fetch:
            with pytest.raises(ValueError, match="ocr must be one of"):
                server.notion_extract_file_text(
                    "notion:blk-1", ocr="tesseract",
                )
        fetch.assert_not_called()

    def test_non_pdf_rejected(self, tmp_files_dir):
        (tmp_files_dir / "notes.txt").write_bytes(b"plain text")
        with pytest.raises(ValueError, match="not a PDF"):
            server.notion_extract_file_text("local:notes.txt")


# ===================================================================
# notion_render_file_page
# ===================================================================


class TestRenderFilePage:
    def test_default_returns_image_content(self, tmp_files_dir):
        from mcp.server.fastmcp.utilities.types import Image

        (tmp_files_dir / "scan.pdf").write_bytes(SCANNED)
        out = server.notion_render_file_page("local:scan.pdf", page=1)
        assert isinstance(out, Image)
        content = out.to_image_content()
        assert content.mimeType == "image/jpeg"

    def test_return_base64_envelope(self, tmp_files_dir):
        (tmp_files_dir / "scan.pdf").write_bytes(SCANNED)
        out = server.notion_render_file_page(
            "local:scan.pdf", page=2, return_base64=True,
        )
        data = json.loads(out)
        assert data["mime_type"] == "image/jpeg"
        assert data["page"] == 2
        assert data["page_count"] == 2
        assert data["filename"] == "scan.pdf"
        import base64 as b64

        assert b64.b64decode(data["image_base64"])[:3] == b"\xff\xd8\xff"

    def test_out_of_range_returns_error_json(self, tmp_files_dir):
        (tmp_files_dir / "scan.pdf").write_bytes(SCANNED)
        out = server.notion_render_file_page("local:scan.pdf", page=99)
        data = json.loads(out)
        assert "out of range" in data["error"]
        assert data["page_count"] == 2

    def test_png_format(self, tmp_files_dir):
        from mcp.server.fastmcp.utilities.types import Image

        (tmp_files_dir / "scan.pdf").write_bytes(SCANNED)
        out = server.notion_render_file_page(
            "local:scan.pdf", page=1, format="png",
        )
        assert isinstance(out, Image)
        assert out.to_image_content().mimeType == "image/png"

    def test_bad_format_raises(self, tmp_files_dir):
        with pytest.raises(ValueError, match="format must be"):
            server.notion_render_file_page("local:x.pdf", format="webp")

    def test_notion_block_source(self):
        from mcp.server.fastmcp.utilities.types import Image

        with patch.object(
            server.notion, "fetch_file_block_bytes",
            return_value=("scan.pdf", "application/pdf", SCANNED),
        ):
            out = server.notion_render_file_page("notion:blk-9")
        assert isinstance(out, Image)


# ===================================================================
# pdf_read surface — the module is mcp_server_kit.pdf now (tested there);
# spot-check that the names this server calls still exist.
# ===================================================================


class TestPdfReadSurface:
    def test_shared_surface_present(self):
        for name in (
            "extract_pdf_text", "render_pdf_page_bytes", "validate_options",
            "parse_page_spec", "assemble_pages", "xberg_ocr_pages",
            "PageOutOfRangeError", "VALID_MODES", "VALID_OCR",
        ):
            assert hasattr(pdf_read, name), f"pdf_read.{name} missing"

    def test_xberg_defaults_when_env_unset(self, monkeypatch):
        for var in ("XBERG_BASE_URL", "XBERG_TIMEOUT"):
            monkeypatch.delenv(var, raising=False)
        base_url, timeout = pdf_read._xberg_config()
        assert base_url == "http://xberg:8000"
        assert timeout == 120.0
