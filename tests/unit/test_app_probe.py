"""Unit tests for AppProbe."""
import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

from agent.orchestration.app_probe import AppProbe, AppHandle, detect_start_command, detect_port


class TestDetectStartCommand:

    def test_detects_package_json_start(self, tmp_path):
        (tmp_path / "package.json").write_text('{"scripts": {"start": "node server.js"}}')
        cmd = detect_start_command(tmp_path)
        assert cmd is not None
        assert "npm" in cmd or "node" in cmd

    def test_detects_python_app_py(self, tmp_path):
        (tmp_path / "app.py").write_text("# flask app")
        cmd = detect_start_command(tmp_path)
        assert cmd is not None
        assert "python" in cmd.lower() or "py" in cmd.lower()

    def test_detects_main_py(self, tmp_path):
        (tmp_path / "main.py").write_text("# main entry")
        cmd = detect_start_command(tmp_path)
        assert cmd is not None

    def test_returns_none_when_no_known_entry(self, tmp_path):
        cmd = detect_start_command(tmp_path)
        assert cmd is None

    def test_npm_start_preferred_over_python(self, tmp_path):
        (tmp_path / "package.json").write_text('{"scripts": {"start": "node server.js"}}')
        (tmp_path / "app.py").write_text("# also here")
        cmd = detect_start_command(tmp_path)
        assert "npm" in cmd

    def test_tauri_dev_preferred_over_npm_start(self, tmp_path):
        (tmp_path / "package.json").write_text('{"scripts": {"start": "node server.js"}}')
        (tmp_path / "src-tauri").mkdir()
        cmd = detect_start_command(tmp_path)
        assert cmd == "npm run tauri dev"

    def test_src_tauri_without_package_json_falls_back(self, tmp_path):
        (tmp_path / "src-tauri").mkdir()
        (tmp_path / "app.py").write_text("# fallback")
        cmd = detect_start_command(tmp_path)
        assert "python" in cmd.lower()

    def test_static_html_with_no_known_entry_gets_static_server(self, tmp_path):
        """A bare index.html (no package.json/server) must not fall through to
        None — it's exactly the "single-file game, no build step" deliverable
        shape this agent produces, and it has to be launchable to be
        acceptance-tested at all."""
        (tmp_path / "index.html").write_text("<!DOCTYPE html><html></html>")
        cmd = detect_start_command(tmp_path)
        assert cmd is not None
        assert "http.server" in cmd

    def test_known_entry_point_preferred_over_static_html(self, tmp_path):
        (tmp_path / "index.html").write_text("<html></html>")
        (tmp_path / "app.py").write_text("# flask app")
        cmd = detect_start_command(tmp_path)
        assert "http.server" not in cmd

    def test_no_html_and_no_known_entry_still_returns_none(self, tmp_path):
        (tmp_path / "README.md").write_text("# not runnable")
        cmd = detect_start_command(tmp_path)
        assert cmd is None


class TestDetectPort:

    def test_reads_port_from_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text('{"scripts": {"start": "node server.js"}}')
        port = detect_port(tmp_path, start_cmd="node server.js")
        assert isinstance(port, (int, type(None)))

    def test_detects_flask_default_port(self, tmp_path):
        (tmp_path / "app.py").write_text("app.run(port=5000)")
        port = detect_port(tmp_path, start_cmd="python app.py")
        assert port == 5000 or port is None

    def test_returns_none_when_unknown(self, tmp_path):
        port = detect_port(tmp_path, start_cmd="./my_binary")
        assert port is None

    def test_recovers_port_from_static_server_command(self, tmp_path):
        port = detect_port(tmp_path, start_cmd="python -m http.server 54321")
        assert port == 54321

    def test_reads_tauri_v2_dev_url(self, tmp_path):
        (tmp_path / "src-tauri").mkdir()
        (tmp_path / "src-tauri" / "tauri.conf.json").write_text(
            '{"build": {"devUrl": "http://localhost:1420"}}'
        )
        port = detect_port(tmp_path, start_cmd="npm run tauri dev")
        assert port == 1420

    def test_reads_tauri_v1_dev_path(self, tmp_path):
        (tmp_path / "src-tauri").mkdir()
        (tmp_path / "src-tauri" / "tauri.conf.json").write_text(
            '{"build": {"devPath": "http://localhost:3000"}}'
        )
        port = detect_port(tmp_path, start_cmd="npm run tauri dev")
        assert port == 3000

    def test_tauri_dev_url_overrides_vite_hint(self, tmp_path):
        (tmp_path / "src-tauri").mkdir()
        (tmp_path / "src-tauri" / "tauri.conf.json").write_text(
            '{"build": {"devUrl": "http://localhost:1420"}}'
        )
        port = detect_port(tmp_path, start_cmd="npm run tauri dev -- --vite")
        assert port == 1420

    def test_malformed_tauri_conf_falls_back(self, tmp_path):
        (tmp_path / "src-tauri").mkdir()
        (tmp_path / "src-tauri" / "tauri.conf.json").write_text("{not valid json")
        port = detect_port(tmp_path, start_cmd="npm run tauri dev")
        assert port is None


class TestAppHandle:

    def test_dataclass_fields(self):
        handle = AppHandle(process=None, port=8080, start_command="npm start")
        assert handle.port == 8080
        assert handle.start_command == "npm start"


class TestAppProbe:

    def test_init_stores_workspace(self, tmp_path):
        probe = AppProbe(tmp_path, shell_fn=AsyncMock())
        assert probe.workspace == tmp_path

    @pytest.mark.asyncio
    async def test_launch_returns_none_when_no_start_command(self, tmp_path):
        probe = AppProbe(tmp_path, shell_fn=AsyncMock())
        handle = await probe.launch()
        assert handle is None

    @pytest.mark.asyncio
    async def test_teardown_is_safe_when_handle_is_none(self, tmp_path):
        probe = AppProbe(tmp_path, shell_fn=AsyncMock())
        # Should not raise
        await probe.teardown(None)

    @pytest.mark.asyncio
    async def test_screenshot_returns_none_when_no_port(self, tmp_path):
        probe = AppProbe(tmp_path, shell_fn=AsyncMock())
        handle = AppHandle(process=None, port=None, start_command="python app.py")
        shot = await probe.screenshot(handle)
        assert shot is None

    @pytest.mark.asyncio
    async def test_launch_serves_static_html_workspace(self, tmp_path):
        """A static single-file deliverable must actually launch via the
        generic static-server fallback, not return None the way it did
        before this fix (which caused acceptance testing to be silently
        skipped for every single-file/no-build-step project)."""
        (tmp_path / "index.html").write_text("<html></html>")
        probe = AppProbe(tmp_path, shell_fn=AsyncMock())

        mock_proc = MagicMock()
        with patch("agent.orchestration.app_probe.subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch.object(AppProbe, "_wait_ready", new=AsyncMock(return_value=True)):
            handle = await probe.launch()

        assert handle is not None
        assert "http.server" in handle.start_command
        assert handle.port is not None
        mock_popen.assert_called_once()
