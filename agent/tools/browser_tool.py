import subprocess
import asyncio
import time
from pathlib import Path
from typing import Any, Optional
import structlog

logger = structlog.get_logger()


class BrowserTool:
    def __init__(self, workspace_path: str):
        # workspace_path comes from server config or a pre-validated path; not raw user HTTP input.
        self.workspace = Path(workspace_path).resolve()
        self.process: Optional[subprocess.Popen] = None
        self.logger = logger.bind(component="browser_tool")
    
    async def start_dev_server(self, port: int = 8080, timeout: int = 30) -> dict:
        """Start the dev server and wait for it to be ready"""
        self.logger.info("starting_dev_server", port=port, workspace=str(self.workspace))
        
        try:
            self.process = subprocess.Popen(
                "npm run start",
                shell=True,
                cwd=str(self.workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            
            # Wait for server to be ready
            for _ in range(timeout):
                await asyncio.sleep(1)
                try:
                    import httpx
                    r = httpx.get(f"http://localhost:{port}", timeout=2)
                    if r.status_code == 200:
                        self.logger.info("dev_server_ready", port=port)
                        return {"success": True, "port": port, "url": f"http://localhost:{port}"}
                except:
                    continue
            
            return {"success": False, "error": "Server failed to start within timeout"}
        except Exception as e:
            self.logger.error("dev_server_error", error=str(e))
            return {"success": False, "error": str(e)}
    
    def stop_dev_server(self) -> None:
        """Stop the dev server"""
        if self.process:
            self.process.terminate()
            self.process = None
            self.logger.info("dev_server_stopped")
    
    async def screenshot(self, url: str = "http://localhost:8080", path: Optional[str] = None, width: int = 1280, height: int = 720) -> dict:
        """Take a screenshot of a URL using Playwright"""
        try:
            from playwright.async_api import async_playwright
            
            if not path:
                path = f"workspace/screenshot_{int(time.time())}.png"
            
            self.logger.info("taking_screenshot", url=url, path=path)
            
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": width, "height": height})
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await page.screenshot(path=path, full_page=False)
                await browser.close()
            
            self.logger.info("screenshot_taken", path=path)
            return {"success": True, "path": path}
        except Exception as e:
            self.logger.error("screenshot_failed", error=str(e))
            return {"success": False, "error": str(e)}
    
    async def wait_for_server(self, url: str, timeout: int = 30) -> bool:
        """Poll *url* until it returns a sub-500 response or *timeout* seconds pass.

        Returns True when the server is up, False on timeout.
        """
        import httpx

        self.logger.info("waiting_for_server", url=url, timeout=timeout)
        for _ in range(timeout):
            await asyncio.sleep(1)
            try:
                r = httpx.get(url, timeout=2)
                if r.status_code < 500:
                    self.logger.info("server_ready", url=url)
                    return True
            except Exception:
                continue
        self.logger.warning("server_wait_timeout", url=url)
        return False

    async def run_and_screenshot(self, port: int = 8080) -> dict:
        """Start dev server, wait, screenshot, stop server"""
        start_result = await self.start_dev_server(port=port)
        if not start_result.get("success"):
            return start_result

        screenshot_result = await self.screenshot(f"http://localhost:{port}")

        self.stop_dev_server()

        return screenshot_result

    async def interact(
        self,
        url: str,
        actions: list[dict[str, Any]],
        timeout: int = 30,
        width: int = 1280,
        height: int = 720,
    ) -> dict[str, Any]:
        """Drive a browser page through a sequence of actions and return a transcript.

        Opens *url* in a headless Chromium instance, executes each action in
        *actions*, then closes the browser.  Works on Windows, macOS, and Linux
        via Playwright's cross-platform API.

        Action types
        ------------
        ``navigate``   — ``{"type": "navigate", "url": "https://..."}``
        ``click``      — ``{"type": "click", "selector": "button#submit"}``
        ``fill``       — ``{"type": "fill",  "selector": "input[name=q]", "value": "hello"}``
        ``press``      — ``{"type": "press", "key": "Enter"}`` (selector optional)
        ``screenshot`` — ``{"type": "screenshot", "path": "out.png"}``  (path optional)
        ``text``       — ``{"type": "text",  "selector": "h1"}``  → appended to transcript
        ``wait_for``   — ``{"type": "wait_for", "selector": ".result", "state": "visible"}``
        ``wait``       — ``{"type": "wait",  "ms": 500}``

        Returns
        -------
        ::

            {
              "success":     bool,
              "transcript":  str,          # human-readable log of each step
              "screenshots": [str, ...],   # paths of any screenshots taken
              "error":       str,          # present only on failure
            }
        """
        transcript: list[str] = []
        screenshots: list[str] = []

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {
                "success": False,
                "error": "playwright is not installed — run: pip install playwright && playwright install chromium",
                "transcript": "",
                "screenshots": [],
            }

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": width, "height": height})

                # Navigate to the starting URL.
                self.logger.info("browser_interact_navigate", url=url)
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                transcript.append(f"[navigate] {url}")

                for i, action in enumerate(actions):
                    atype = action.get("type", "")

                    if atype == "navigate":
                        dest = action["url"]
                        await page.goto(dest, wait_until="domcontentloaded", timeout=timeout * 1000)
                        transcript.append(f"[navigate] {dest}")

                    elif atype == "click":
                        sel = action["selector"]
                        await page.click(sel, timeout=timeout * 1000)
                        transcript.append(f"[click] {sel}")

                    elif atype == "fill":
                        sel = action["selector"]
                        val = action.get("value", "")
                        await page.fill(sel, val, timeout=timeout * 1000)
                        transcript.append(f"[fill] {sel} = {val!r}")

                    elif atype == "press":
                        key = action["key"]
                        sel = action.get("selector")
                        if sel:
                            await page.press(sel, key, timeout=timeout * 1000)
                        else:
                            await page.keyboard.press(key)
                        transcript.append(f"[press] {key}" + (f" on {sel}" if sel else ""))

                    elif atype == "screenshot":
                        path = action.get("path") or f"screenshot_{int(time.time())}_{i}.png"
                        await page.screenshot(path=path, full_page=False)
                        screenshots.append(path)
                        transcript.append(f"[screenshot] saved to {path}")

                    elif atype == "text":
                        sel = action["selector"]
                        text = await page.text_content(sel, timeout=timeout * 1000) or ""
                        transcript.append(f"[text] {sel} → {text.strip()[:200]}")

                    elif atype == "wait_for":
                        sel = action["selector"]
                        state = action.get("state", "visible")
                        await page.wait_for_selector(sel, state=state, timeout=timeout * 1000)
                        transcript.append(f"[wait_for] {sel} state={state}")

                    elif atype == "wait":
                        ms = int(action.get("ms", 500))
                        await asyncio.sleep(ms / 1000)
                        transcript.append(f"[wait] {ms}ms")

                    else:
                        transcript.append(f"[unknown action type {atype!r} at step {i} — skipped]")

                await browser.close()

            self.logger.info(
                "browser_interact_done",
                steps=len(actions),
                screenshots=len(screenshots),
            )
            return {
                "success": True,
                "transcript": "\n".join(transcript),
                "screenshots": screenshots,
            }

        except Exception as exc:
            self.logger.error("browser_interact_error", error=str(exc))
            return {
                "success": False,
                "error": str(exc),
                "transcript": "\n".join(transcript),
                "screenshots": screenshots,
            }


__all__ = ["BrowserTool"]