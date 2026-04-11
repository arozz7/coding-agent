"""Web browsing and search tool.

Uses Playwright (Chromium) for JS-rendered pages with an httpx + BeautifulSoup
fallback for simple HTML.  Search is provided by DuckDuckGo (no API key needed).

All text is truncated before returning to avoid flooding the context window.
"""

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import structlog

logger = structlog.get_logger()

_MAX_PAGE_CHARS = 8_000   # cap per fetched page
_MAX_RESULTS = 8           # cap for search results


class WebTool:
    """Provides web fetch, search, and URL screenshot capabilities."""

    def __init__(self):
        self.logger = logger.bind(component="web_tool")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def fetch_url(self, url: str) -> Dict[str, Any]:
        """Fetch a URL and return its rendered text content.

        Tries Playwright first (handles JS-heavy pages); falls back to
        httpx + BeautifulSoup for plain HTML.
        """
        try:
            return await self._fetch_playwright(url)
        except Exception as pw_err:
            self.logger.warning("playwright_unavailable", error=str(pw_err)[:120])
            return await self._fetch_httpx(url)

    async def search(self, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        """Search the web. Uses Google Custom Search if credentials are configured,
        otherwise falls back to DuckDuckGo (no API key required)."""
        google_key = os.environ.get("GOOGLE_SEARCH_API_KEY", "")
        google_cx = os.environ.get("GOOGLE_SEARCH_CX", "")
        if google_key and google_cx:
            results = await self._search_google(query, max_results, google_key, google_cx)
            if results and "error" not in results[0]:
                return results
            self.logger.warning("google_search_failed_fallback_duckduckgo")

        return await self._search_duckduckgo(query, max_results)

    async def _search_google(
        self,
        query: str,
        max_results: int,
        api_key: str,
        cx: str,
    ) -> List[Dict[str, str]]:
        """Call the Google Custom Search JSON API."""
        n = min(max_results, 10)  # Google API max is 10 per request
        params = {"key": api_key, "cx": cx, "q": query, "num": n}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(
                    "https://www.googleapis.com/customsearch/v1", params=params
                )
                r.raise_for_status()
                data = r.json()
            return [
                {
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": (item.get("snippet") or "")[:400],
                }
                for item in data.get("items", [])
            ]
        except Exception as e:
            self.logger.error("google_search_failed", query=query, error=str(e))
            return [{"error": str(e)}]

    async def _search_duckduckgo(
        self, query: str, max_results: int
    ) -> List[Dict[str, str]]:
        """Fall back to DuckDuckGo (no API key required)."""
        try:
            from duckduckgo_search import DDGS  # type: ignore
        except ImportError:
            return [{"error": "duckduckgo-search not installed — run: pip install duckduckgo-search"}]

        try:
            n = min(max_results, _MAX_RESULTS)
            raw: list = await asyncio.to_thread(
                lambda: list(DDGS().text(query, max_results=n))
            )
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": (r.get("body") or "")[:400],
                }
                for r in raw
            ]
        except Exception as e:
            self.logger.error("duckduckgo_search_failed", query=query, error=str(e))
            return [{"error": str(e)}]

    async def screenshot_url(
        self, url: str, output_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """Take a Playwright screenshot of any URL."""
        if not output_path:
            output_path = str(Path("workspace") / f"screenshot_{int(time.time())}.png")
        try:
            from playwright.async_api import async_playwright  # type: ignore

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1280, "height": 720})
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await page.screenshot(path=output_path, full_page=False)
                await browser.close()

            return {"success": True, "path": output_path, "url": url}
        except Exception as e:
            self.logger.error("screenshot_url_failed", url=url, error=str(e))
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_playwright(self, url: str) -> Dict[str, Any]:
        from playwright.async_api import async_playwright  # type: ignore

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                title = await page.title()
                # Strip nav/footer/aside clutter, grab remaining body text
                text: str = await page.evaluate(
                    """() => {
                        document.querySelectorAll(
                            'script, style, nav, footer, aside, [aria-hidden="true"]'
                        ).forEach(e => e.remove());
                        return document.body ? document.body.innerText : '';
                    }"""
                )
                text = _clean_text(text)
                return {
                    "success": True,
                    "url": url,
                    "title": title,
                    "text": text[:_MAX_PAGE_CHARS],
                    "truncated": len(text) > _MAX_PAGE_CHARS,
                    "source": "playwright",
                }
            finally:
                await browser.close()

    async def _fetch_httpx(self, url: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"},
        ) as client:
            r = await client.get(url)
            r.raise_for_status()
            html = r.text

        try:
            from bs4 import BeautifulSoup  # type: ignore

            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "aside"]):
                tag.decompose()
            title = soup.title.string.strip() if soup.title else url
            text = _clean_text(soup.get_text(separator="\n"))
        except ImportError:
            title = url
            text = _clean_text(html)

        return {
            "success": True,
            "url": url,
            "title": title,
            "text": text[:_MAX_PAGE_CHARS],
            "truncated": len(text) > _MAX_PAGE_CHARS,
            "source": "httpx",
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def extract_urls(text: str) -> List[str]:
    """Return all http/https URLs found in *text*."""
    return re.findall(r"https?://[^\s\)\]\>\"']+", text)


__all__ = ["WebTool", "extract_urls"]
