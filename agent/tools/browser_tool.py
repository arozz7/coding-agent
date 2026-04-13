import subprocess
import asyncio
import time
from pathlib import Path
from typing import Optional
import structlog

logger = structlog.get_logger()


class BrowserTool:
    def __init__(self, workspace_path: str):
        # workspace_path comes from server config or a pre-validated path; not raw user HTTP input.
        self.workspace = Path(workspace_path).resolve()  # lgtm[py/path-injection]
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


__all__ = ["BrowserTool"]