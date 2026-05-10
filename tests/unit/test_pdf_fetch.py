"""Unit tests for PDF fetch functionality (DocumentTool.read_pdf_url, pdf_fetch tool)."""
import io
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.document_tool import DocumentTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_pdfplumber(text_per_page: list[str]):
    """Return a mock pdfplumber context manager producing the given pages."""
    pages = []
    for t in text_per_page:
        p = MagicMock()
        p.extract_text.return_value = t
        pages.append(p)
    pdf_mock = MagicMock()
    pdf_mock.pages = pages
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=pdf_mock)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# DocumentTool.read_pdf_url
# ---------------------------------------------------------------------------

class TestDocumentToolReadPdfUrl:

    def test_success_returns_text(self, tmp_path):
        tool = DocumentTool()
        fake_pdf = _make_fake_pdfplumber(["Page one text", "Page two text"])
        with patch("agent.tools.document_tool.DocumentTool._extract_pdf", wraps=tool._extract_pdf):
            with patch("urllib.request.urlretrieve") as mock_dl, \
                 patch("pdfplumber.open", return_value=fake_pdf):
                # Make urlretrieve write nothing (file created by NamedTemporaryFile)
                mock_dl.return_value = (None, {})
                result = tool.read_pdf_url("https://arxiv.org/pdf/2503.13657")

        assert result["success"] is True
        assert "Page one text" in result["text"]
        assert result["url"] == "https://arxiv.org/pdf/2503.13657"
        assert result["type"] == "pdf"

    def test_download_failure_returns_error(self):
        tool = DocumentTool()
        with patch("urllib.request.urlretrieve", side_effect=OSError("network error")):
            result = tool.read_pdf_url("https://arxiv.org/pdf/bad.pdf")
        assert result["success"] is False
        assert "error" in result

    def test_temp_file_cleaned_up_on_success(self, tmp_path):
        tool = DocumentTool()
        created_tmp: list[str] = []

        original_extract = tool._extract_pdf

        def capture_and_extract(path: Path):
            created_tmp.append(str(path))
            return {"success": True, "type": "pdf", "total_pages": 1,
                    "text": "content", "truncated": False, "path": str(path)}

        with patch("urllib.request.urlretrieve"), \
             patch.object(tool, "_extract_pdf", side_effect=capture_and_extract):
            tool.read_pdf_url("https://example.com/paper.pdf")

        # Temp file should be gone after the call
        for p in created_tmp:
            assert not Path(p).exists(), f"Temp file not cleaned up: {p}"

    def test_temp_file_cleaned_up_on_failure(self):
        tool = DocumentTool()
        created_tmp: list[str] = []

        def capture_and_fail(path: Path):
            created_tmp.append(str(path))
            raise RuntimeError("parse error")

        with patch("urllib.request.urlretrieve"), \
             patch.object(tool, "_extract_pdf", side_effect=capture_and_fail):
            result = tool.read_pdf_url("https://example.com/paper.pdf")

        assert result["success"] is False
        for p in created_tmp:
            assert not Path(p).exists(), f"Temp file not cleaned up: {p}"

    def test_multi_page_text_joined(self):
        tool = DocumentTool()
        fake_pdf = _make_fake_pdfplumber(["Alpha", "Beta", "Gamma"])
        with patch("urllib.request.urlretrieve"), \
             patch("pdfplumber.open", return_value=fake_pdf):
            result = tool.read_pdf_url("https://example.com/doc.pdf")
        assert result["success"] is True
        assert "Alpha" in result["text"]
        assert "Beta" in result["text"]
        assert result["total_pages"] == 3


# ---------------------------------------------------------------------------
# ToolExecutor pdf_fetch tool
# ---------------------------------------------------------------------------

class TestPdfFetchTool:

    def _make_executor(self):
        from agent.tools.tool_executor import ToolExecutor
        with tempfile.TemporaryDirectory() as tmpdir:
            ex = ToolExecutor.__new__(ToolExecutor)
            ex.workspace_path = tmpdir
            ex.tools = {}
            ex.logger = MagicMock()
            ex.document_tool = DocumentTool()
            ex.tools["pdf_fetch"] = ex._pdf_fetch
            return ex

    def test_returns_text_on_success(self):
        ex = self._make_executor()
        ex.document_tool.read_pdf_url = MagicMock(return_value={
            "success": True, "type": "pdf", "total_pages": 5,
            "text": "Deep learning advances", "truncated": False,
        })
        result = ex._pdf_fetch({"url": "https://arxiv.org/pdf/2503.13657"})
        assert "Deep learning advances" in result
        assert "5 pages" in result

    def test_missing_url_returns_error(self):
        ex = self._make_executor()
        result = ex._pdf_fetch({})
        assert result.startswith("Error: 'url' is required")

    def test_document_tool_failure_returns_error(self):
        ex = self._make_executor()
        ex.document_tool.read_pdf_url = MagicMock(return_value={
            "success": False, "error": "corrupt PDF",
        })
        result = ex._pdf_fetch({"url": "https://example.com/bad.pdf"})
        assert "PDF fetch failed" in result
        assert "corrupt PDF" in result

    def test_truncated_flag_shown(self):
        ex = self._make_executor()
        ex.document_tool.read_pdf_url = MagicMock(return_value={
            "success": True, "type": "pdf", "total_pages": 50,
            "text": "x" * 10_000, "truncated": True,
        })
        result = ex._pdf_fetch({"url": "https://example.com/big.pdf"})
        assert "[truncated]" in result


# ---------------------------------------------------------------------------
# ResearchRole._search_question — PDF URL branch
# ---------------------------------------------------------------------------

class TestSearchQuestionPdfBranch:

    def _make_role(self):
        from agent.agents.research_agent import ResearchRole
        return ResearchRole()

    @pytest.mark.asyncio
    async def test_pdf_url_calls_pdf_fetch(self):
        role = self._make_role()
        tool_executor = MagicMock()
        tool_executor.execute = AsyncMock(side_effect=lambda name, inp: (
            "1. Result https://arxiv.org/pdf/2503.13657v1"
            if name == "web_search" else
            "[PDF: arxiv...]\n\nPaper content here"
        ))
        result = await role._search_question("self-improving agents", tool_executor)
        calls = [c.args[0] for c in tool_executor.execute.call_args_list]
        assert "pdf_fetch" in calls
        assert "web_fetch" not in calls
        assert "Paper content here" in result

    @pytest.mark.asyncio
    async def test_normal_url_calls_web_fetch(self):
        role = self._make_role()
        tool_executor = MagicMock()
        tool_executor.execute = AsyncMock(side_effect=lambda name, inp: (
            "1. Result https://example.com/article"
            if name == "web_search" else
            "Article content " * 100
        ))
        result = await role._search_question("machine learning trends", tool_executor)
        calls = [c.args[0] for c in tool_executor.execute.call_args_list]
        assert "web_fetch" in calls
        assert "pdf_fetch" not in calls

    @pytest.mark.asyncio
    async def test_pdf_fetch_error_does_not_raise(self):
        role = self._make_role()
        tool_executor = MagicMock()
        tool_executor.execute = AsyncMock(side_effect=lambda name, inp: (
            "1. Result https://arxiv.org/pdf/0000.00000"
            if name == "web_search" else (_ for _ in ()).throw(RuntimeError("download failed"))
        ))
        # Should not raise — errors in pdf_fetch are swallowed
        result = await role._search_question("test query", tool_executor)
        assert isinstance(result, str)
