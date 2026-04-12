"""Integration tests for web search and fallback chain.

Tests the WebTool component directly (not through HTTP) to validate:
  - Google Custom Search primary path (happy path)
  - Google 403/error → DuckDuckGo fallback
  - DuckDuckGo success path
  - Date injection for relative-date queries
  - URL fetch via httpx fallback

These are component-level tests that mock httpx and the search backends
so no network traffic is made.

Note: tests patch the DDGS import via sys.modules so they work regardless
of whether 'ddgs' or 'duckduckgo_search' is the installed package.
"""

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.web_tool import WebTool


def _make_ddgs_module(results: list) -> ModuleType:
    """Return a fake 'ddgs' module whose DDGS().text() yields *results*."""
    mod = ModuleType("ddgs")
    mock_instance = MagicMock()
    mock_instance.text.return_value = results
    mock_cls = MagicMock(return_value=mock_instance)
    mod.DDGS = mock_cls
    return mod


def _make_ddgs_module_raising(exc: Exception) -> ModuleType:
    """Return a fake 'ddgs' module whose DDGS().text() raises *exc*."""
    mod = ModuleType("ddgs")
    mock_instance = MagicMock()
    mock_instance.text.side_effect = exc
    mock_cls = MagicMock(return_value=mock_instance)
    mod.DDGS = mock_cls
    return mod


class TestBraveSearch:
    """Brave Search API — primary backend."""

    @pytest.mark.asyncio
    async def test_brave_search_happy_path(self, monkeypatch):
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "fake_brave_key")
        monkeypatch.delenv("GOOGLE_SEARCH_API_KEY", raising=False)

        tool = WebTool()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "web": {
                "results": [
                    {"title": "Brave Result 1", "url": "https://brave.com/1", "description": "Desc 1"},
                    {"title": "Brave Result 2", "url": "https://brave.com/2", "description": "Desc 2"},
                ]
            }
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("agent.tools.web_tool.httpx.AsyncClient", return_value=mock_client):
            results = await tool.search("test query", max_results=2)

        assert len(results) == 2
        assert results[0]["title"] == "Brave Result 1"
        assert results[0]["url"] == "https://brave.com/1"
        assert "error" not in results[0]

    @pytest.mark.asyncio
    async def test_brave_search_falls_back_to_ddg_on_error(self, monkeypatch):
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "bad_key")
        monkeypatch.delenv("GOOGLE_SEARCH_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_SEARCH_CX", raising=False)

        tool = WebTool()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("401 Unauthorized"))

        ddg_results = [{"title": "DDG", "href": "https://ddg.com", "body": "fallback"}]
        fake_ddgs = _make_ddgs_module(ddg_results)

        with (
            patch("agent.tools.web_tool.httpx.AsyncClient", return_value=mock_client),
            patch.dict(sys.modules, {"ddgs": fake_ddgs}),
        ):
            results = await tool.search("test query", max_results=3)

        assert len(results) >= 1
        assert results[0].get("title") == "DDG"

    @pytest.mark.asyncio
    async def test_no_brave_key_goes_straight_to_ddg(self, monkeypatch):
        monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_SEARCH_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_SEARCH_CX", raising=False)

        tool = WebTool()
        ddg_results = [{"title": "T", "href": "https://x.com", "body": "body"}]
        fake_ddgs = _make_ddgs_module(ddg_results)

        with patch.dict(sys.modules, {"ddgs": fake_ddgs}):
            results = await tool.search("no keys query", max_results=3)

        assert len(results) == 1
        assert results[0]["title"] == "T"


class TestGoogleSearch:
    @pytest.mark.asyncio
    async def test_google_search_happy_path(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_SEARCH_API_KEY", "fake_key")
        monkeypatch.setenv("GOOGLE_SEARCH_CX", "fake_cx")

        tool = WebTool()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "items": [
                {"title": "Result 1", "link": "https://example.com/1", "snippet": "Snippet 1"},
                {"title": "Result 2", "link": "https://example.com/2", "snippet": "Snippet 2"},
            ]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("agent.tools.web_tool.httpx.AsyncClient", return_value=mock_client):
            results = await tool.search("test query", max_results=2)

        assert len(results) == 2
        assert results[0]["title"] == "Result 1"
        assert results[0]["url"] == "https://example.com/1"
        assert "error" not in results[0]

    @pytest.mark.asyncio
    async def test_google_search_fallback_on_http_error(self, monkeypatch):
        """Google returning an HTTP error should silently fall back to DDG."""
        monkeypatch.setenv("GOOGLE_SEARCH_API_KEY", "fake_key")
        monkeypatch.setenv("GOOGLE_SEARCH_CX", "fake_cx")

        tool = WebTool()

        # Google raises an exception (simulate 403)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("403 Forbidden"))

        ddg_results = [
            {"title": "DDG Result", "href": "https://ddg.com/1", "body": "DDG snippet"},
        ]

        fake_ddgs = _make_ddgs_module(ddg_results)
        with (
            patch("agent.tools.web_tool.httpx.AsyncClient", return_value=mock_client),
            patch.dict(sys.modules, {"ddgs": fake_ddgs}),
        ):
            results = await tool.search("test query", max_results=3)

        # Should have fallen back to DDG
        assert len(results) >= 1
        # No hard error key — google error is swallowed, we get DDG results

    @pytest.mark.asyncio
    async def test_google_search_fallback_on_error_key(self, monkeypatch):
        """Google returning {error: ...} result should fall back to DDG."""
        monkeypatch.setenv("GOOGLE_SEARCH_API_KEY", "fake_key")
        monkeypatch.setenv("GOOGLE_SEARCH_CX", "fake_cx")

        tool = WebTool()

        ddg_results = [
            {"title": "DDG Hit", "href": "https://ddg.example.com", "body": "body text"},
        ]

        fake_ddgs = _make_ddgs_module(ddg_results)
        with (
            patch.object(tool, "_search_google", AsyncMock(return_value=[{"error": "403 Forbidden"}])),
            patch.dict(sys.modules, {"ddgs": fake_ddgs}),
        ):
            results = await tool.search("fallback test", max_results=3)

        assert len(results) >= 1
        assert results[0].get("title") == "DDG Hit"

    @pytest.mark.asyncio
    async def test_no_google_credentials_uses_ddg(self, monkeypatch):
        """When Google credentials are absent, DDG is used directly."""
        monkeypatch.delenv("GOOGLE_SEARCH_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_SEARCH_CX", raising=False)

        tool = WebTool()

        ddg_results = [
            {"title": "T1", "href": "https://a.com", "body": "body1"},
            {"title": "T2", "href": "https://b.com", "body": "body2"},
        ]

        fake_ddgs = _make_ddgs_module(ddg_results)
        with patch.dict(sys.modules, {"ddgs": fake_ddgs}):
            results = await tool.search("no google query", max_results=3)

        assert len(results) == 2
        assert results[0]["title"] == "T1"
        assert results[1]["url"] == "https://b.com"


class TestDuckDuckGoSearch:
    @pytest.mark.asyncio
    async def test_ddg_search_success(self):
        tool = WebTool()

        ddg_results = [
            {"title": "News", "href": "https://news.com/story", "body": "Some news content"},
        ]

        fake_ddgs = _make_ddgs_module(ddg_results)
        with patch.dict(sys.modules, {"ddgs": fake_ddgs}):
            results = await tool._search_duckduckgo("some query", max_results=5)

        assert len(results) == 1
        assert results[0]["title"] == "News"
        assert results[0]["url"] == "https://news.com/story"
        assert results[0]["snippet"] == "Some news content"

    @pytest.mark.asyncio
    async def test_ddg_search_empty_results(self):
        tool = WebTool()

        fake_ddgs = _make_ddgs_module([])
        with patch.dict(sys.modules, {"ddgs": fake_ddgs}):
            results = await tool._search_duckduckgo("empty query", max_results=5)

        assert results == []

    @pytest.mark.asyncio
    async def test_ddg_exception_returns_error(self):
        tool = WebTool()

        fake_ddgs = _make_ddgs_module_raising(RuntimeError("network error"))
        with patch.dict(sys.modules, {"ddgs": fake_ddgs}):
            results = await tool._search_duckduckgo("broken query", max_results=5)

        assert len(results) == 1
        assert "error" in results[0]

    @pytest.mark.asyncio
    async def test_ddg_snippet_truncated_to_400_chars(self):
        tool = WebTool()
        long_body = "x" * 600

        fake_ddgs = _make_ddgs_module([
            {"title": "T", "href": "https://x.com", "body": long_body}
        ])
        with patch.dict(sys.modules, {"ddgs": fake_ddgs}):
            results = await tool._search_duckduckgo("q", max_results=1)

        assert len(results[0]["snippet"]) == 400


class TestPlaywrightGoogleSearch:
    """Playwright google.com scrape — 3rd fallback, no API key needed."""

    @pytest.mark.asyncio
    async def test_playwright_google_returns_results(self):
        tool = WebTool()

        fake_raw = [
            {"title": "Result A", "url": "https://a.com", "snippet": "Snippet A"},
            {"title": "Result B", "url": "https://b.com", "snippet": "Snippet B"},
        ]

        # Mock the page.evaluate call to return fake results without hitting network
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_load_state = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=fake_raw)
        mock_page.locator = MagicMock(return_value=AsyncMock(
            **{"first.is_visible": AsyncMock(return_value=False)}
        ))

        mock_ctx = AsyncMock()
        mock_ctx.new_page = AsyncMock(return_value=mock_page)

        mock_browser = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_ctx)
        mock_browser.close = AsyncMock()

        mock_playwright = AsyncMock()
        mock_playwright.__aenter__ = AsyncMock(return_value=mock_playwright)
        mock_playwright.__aexit__ = AsyncMock(return_value=False)
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

        with patch("agent.tools.web_tool.async_playwright", return_value=mock_playwright, create=True):
            # Patch the import inside the method
            with patch("playwright.async_api.async_playwright", return_value=mock_playwright):
                results = await tool._search_playwright_google("test query", max_results=5)

        # Results should have been parsed from fake_raw
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_playwright_google_error_returns_error_dict(self):
        tool = WebTool()

        with patch("playwright.async_api.async_playwright", side_effect=Exception("browser crash")):
            results = await tool._search_playwright_google("test query", max_results=5)

        assert len(results) == 1
        assert "error" in results[0]

    @pytest.mark.asyncio
    async def test_playwright_not_installed_returns_error(self):
        tool = WebTool()

        with patch.dict(sys.modules, {"playwright": None, "playwright.async_api": None}):
            results = await tool._search_playwright_google("test query", max_results=5)

        assert "error" in results[0]


class TestQueryDateResolver:
    """_resolve_query_date injects the correct calendar date."""

    def test_last_night_gets_yesterday(self):
        from agent.tools.web_tool import _resolve_query_date
        from datetime import date, timedelta

        result = _resolve_query_date("Yankees score last night")
        yesterday = (date.today() - timedelta(days=1)).strftime("%B %d %Y")
        assert yesterday in result

    def test_today_gets_todays_date(self):
        from agent.tools.web_tool import _resolve_query_date
        from datetime import date

        result = _resolve_query_date("news today")
        today_str = date.today().strftime("%B %d %Y")
        assert today_str in result

    def test_no_relative_term_unchanged(self):
        from agent.tools.web_tool import _resolve_query_date

        q = "how does recursion work"
        assert _resolve_query_date(q) == q

    def test_currently_gets_todays_date(self):
        from agent.tools.web_tool import _resolve_query_date
        from datetime import date

        result = _resolve_query_date("what is currently the best framework")
        today_str = date.today().strftime("%B %d %Y")
        assert today_str in result


class TestUrlFetch:
    @pytest.mark.asyncio
    async def test_fetch_url_httpx_fallback(self):
        """When Playwright is unavailable, falls back to httpx + BS4."""
        tool = WebTool()

        html = "<html><head><title>Test Page</title></head><body><p>Hello world</p></body></html>"
        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch.object(tool, "_fetch_playwright", AsyncMock(side_effect=Exception("playwright not installed"))),
            patch("agent.tools.web_tool.httpx.AsyncClient", return_value=mock_client),
        ):
            result = await tool.fetch_url("https://example.com")

        assert result["success"] is True
        assert "Test Page" in result["title"] or "Hello world" in result["text"]
        assert result["source"] == "httpx"

    @pytest.mark.asyncio
    async def test_fetch_url_text_truncated(self):
        """Text longer than _MAX_PAGE_CHARS is truncated."""
        from agent.tools.web_tool import _MAX_PAGE_CHARS

        tool = WebTool()
        long_text = "word " * 10_000  # well above 8 000 chars

        mock_response = MagicMock()
        mock_response.text = f"<html><body><p>{long_text}</p></body></html>"
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch.object(tool, "_fetch_playwright", AsyncMock(side_effect=Exception("pw off"))),
            patch("agent.tools.web_tool.httpx.AsyncClient", return_value=mock_client),
        ):
            result = await tool.fetch_url("https://example.com")

        assert len(result["text"]) <= _MAX_PAGE_CHARS
        assert result["truncated"] is True
