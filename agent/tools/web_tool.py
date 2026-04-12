"""Web browsing and search tool.

Search priority (first available wins):
  1. Brave Search API  — full web, 2 000 free queries/month, set BRAVE_SEARCH_API_KEY
  2. DuckDuckGo        — unlimited, no key required, always-on fallback
  3. Google CSE        — deprecated for full-web search as of Jan 2026; kept as
                         last-resort for the OWUI engine that still works until 2027

Page fetching:
  Playwright (JS-rendered) → httpx + BeautifulSoup (plain HTML fallback)

All text is truncated before returning to avoid flooding the context window.
"""

import asyncio
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import structlog

logger = structlog.get_logger()

_MAX_PAGE_CHARS = 8_000   # cap per fetched page
_MAX_RESULTS = 8           # cap for search results

# Relative date terms that need to be resolved before passing to search engines.
# A query containing any of these gets the actual date appended so search engines
# return results from the correct timeframe instead of guessing.
_RELATIVE_DATE_RE = re.compile(
    r"\b(last\s+night|yesterday|today|tonight|"
    r"this\s+morning|this\s+afternoon|this\s+evening|"
    r"this\s+week|last\s+week|this\s+month|last\s+month|"
    r"right\s+now|just\s+now|at\s+the\s+moment|currently)\b",
    re.IGNORECASE,
)


def _resolve_query_date(query: str) -> str:
    """Append an explicit date to queries that contain relative date terms.

    Examples
    --------
    "Yankees score last night"  →  "Yankees score last night April 11 2026"
    "latest news today"         →  "latest news today April 12 2026"

    Queries with no relative terms are returned unchanged.
    """
    if not _RELATIVE_DATE_RE.search(query):
        return query

    today = date.today()
    yesterday = today - timedelta(days=1)

    lower = query.lower()
    if "last night" in lower or "yesterday" in lower:
        date_hint = yesterday.strftime("%B %d %Y")
    else:
        date_hint = today.strftime("%B %d %Y")

    return f"{query} {date_hint}"


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
        """Search the web using the best available backend.

        Priority:
          1. Brave Search API  — set BRAVE_SEARCH_API_KEY (2 000 free/month)
          2. DuckDuckGo        — no key needed, always available
          3. Playwright Google — browser scrape of google.com, no key needed,
                                 requires playwright + chromium to be installed
          4. Google CSE API    — deprecated for full-web as of Jan 2026, last resort

        Relative date terms in *query* ("last night", "today", etc.) are
        expanded to actual calendar dates before the query is sent so that
        search engines return results from the correct timeframe.
        """
        query = _resolve_query_date(query)

        # 1. Brave Search API
        brave_key = os.environ.get("BRAVE_SEARCH_API_KEY", "")
        if brave_key:
            results = await self._search_brave(query, max_results, brave_key)
            if results and "error" not in results[0]:
                return results
            self.logger.warning("brave_search_failed_fallback_ddg")

        # 2. DuckDuckGo
        results = await self._search_duckduckgo(query, max_results)
        if results and "error" not in results[0]:
            return results
        self.logger.warning("duckduckgo_failed_fallback_playwright_google")

        # 3. Playwright Google scrape (no API key, uses the real google.com)
        playwright_results = await self._search_playwright_google(query, max_results)
        if playwright_results and "error" not in playwright_results[0]:
            return playwright_results
        self.logger.warning("playwright_google_failed_fallback_google_cse")

        # 4. Google CSE (deprecated, last resort)
        google_key = os.environ.get("GOOGLE_SEARCH_API_KEY", "")
        google_cx = os.environ.get("GOOGLE_SEARCH_CX", "")
        if google_key and google_cx:
            return await self._search_google(query, max_results, google_key, google_cx)

        # Return the last error so callers can surface it
        return playwright_results if playwright_results else results

    # ------------------------------------------------------------------
    # Search backends
    # ------------------------------------------------------------------

    async def _search_brave(
        self, query: str, max_results: int, api_key: str
    ) -> List[Dict[str, str]]:
        """Brave Search API — https://api.search.brave.com/res/v1/web/search"""
        n = min(max_results, 20)
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        }
        params = {"q": query, "count": n, "safesearch": "moderate"}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    headers=headers,
                    params=params,
                )
                r.raise_for_status()
                data = r.json()
            return [
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": (item.get("description") or "")[:400],
                }
                for item in data.get("web", {}).get("results", [])
            ]
        except Exception as e:
            self.logger.error("brave_search_failed", query=query, error=str(e))
            return [{"error": str(e)}]

    async def _search_duckduckgo(
        self, query: str, max_results: int
    ) -> List[Dict[str, str]]:
        """DuckDuckGo — no API key required."""
        try:
            from ddgs import DDGS  # type: ignore
        except ImportError:
            try:
                from duckduckgo_search import DDGS  # type: ignore  # legacy name
            except ImportError:
                return [{"error": "ddgs not installed — run: pip install ddgs"}]

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

    async def _search_google(
        self,
        query: str,
        max_results: int,
        api_key: str,
        cx: str,
    ) -> List[Dict[str, str]]:
        """Google Custom Search JSON API (deprecated for full-web, last resort)."""
        n = min(max_results, 10)
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

    async def _search_playwright_google(
        self, query: str, max_results: int
    ) -> List[Dict[str, str]]:
        """Scrape Google.com search results using Playwright.

        No API key required — uses the real google.com with a browser.
        Falls back gracefully if Playwright / Chromium is not installed.
        """
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError:
            return [{"error": "playwright not installed"}]

        import urllib.parse
        url = (
            "https://www.google.com/search?"
            + urllib.parse.urlencode({"q": query, "num": min(max_results, 10), "hl": "en"})
        )

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    ctx = await browser.new_context(
                        # Realistic desktop UA to avoid trivial bot blocks
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        locale="en-US",
                    )
                    page = await ctx.new_page()
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

                    # Dismiss cookie/consent overlay if present (EU regions)
                    try:
                        accept_btn = page.locator(
                            "button:has-text('Accept all'), button:has-text('I agree')"
                        ).first
                        if await accept_btn.is_visible(timeout=2_000):
                            await accept_btn.click()
                            await page.wait_for_load_state("domcontentloaded")
                    except Exception:
                        pass

                    # Extract organic results via JS evaluation.
                    # Google's class names are obfuscated but the structure is stable:
                    #   each result lives in a div.g (or similar) containing an <h3>
                    #   and a parent <a> that holds the real URL.
                    raw: list = await page.evaluate(
                        """() => {
                            const out = [];
                            // Collect all h3 elements inside anchor tags — these are titles
                            document.querySelectorAll('a:has(h3)').forEach(a => {
                                const h3 = a.querySelector('h3');
                                if (!h3) return;
                                const href = a.href || '';
                                // Skip Google-internal navigation links
                                if (!href.startsWith('http') || href.includes('google.com/search')) return;
                                // Snippet: nearest sibling div with a reasonable amount of text
                                let snippet = '';
                                const parent = a.closest('div[data-hveid], div.g, div[jscontroller]');
                                if (parent) {
                                    const spans = parent.querySelectorAll('span, div');
                                    for (const el of spans) {
                                        const t = el.innerText || '';
                                        if (t.length > 60 && t.length < 600 && !el.querySelector('h3')) {
                                            snippet = t.slice(0, 400);
                                            break;
                                        }
                                    }
                                }
                                out.push({ title: h3.innerText, url: href, snippet });
                            });
                            return out;
                        }"""
                    )
                finally:
                    await browser.close()

            results = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": (r.get("snippet") or "")[:400],
                }
                for r in raw
                if r.get("title") and r.get("url")
            ][:max_results]

            if not results:
                return [{"error": "playwright_google returned no results"}]

            self.logger.info(
                "playwright_google_success",
                query=query[:60],
                result_count=len(results),
            )
            return results

        except Exception as e:
            self.logger.error("playwright_google_failed", query=query[:60], error=str(e))
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
