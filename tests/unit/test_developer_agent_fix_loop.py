"""Characterization tests for DeveloperRole.execute()'s fix-and-rerun loop.

Written before extracting the fix loop out of developer_agent.py (Phase C
task 6, docs/plans/codebase-improvement-plan.md) because the loop had zero
existing coverage: TestDeveloperRole in test_agents.py never passes a
tool_executor (so every branch gated by `if tool_executor:` is skipped
entirely), and the SDLC integration tests mock orch.developer_agent.run()
wholesale rather than exercising DeveloperRole.execute()'s internals.

These tests exist to give the extraction something real to verify against,
not to be an exhaustive spec of every fix-loop branch.
"""
from unittest.mock import AsyncMock, Mock

import pytest

from agent.agents.developer_agent import DeveloperRole


class _FakeModelRouter:
    """Returns queued responses in order; records every prompt it was called with."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[str] = []
        self.get_model = Mock(return_value=Mock())

    async def generate(self, prompt, model, system_prompt=None, **kwargs):
        self.calls.append(prompt)
        if not self._responses:
            raise AssertionError("_FakeModelRouter ran out of queued responses")
        return self._responses.pop(0)


class _FakeToolExecutor:
    """Dispatches shell/file_read/file_write/file_edit like the real ToolExecutor.

    shell_fn(cmd) -> str lets each test control what a shell command returns,
    including varying by call count. file_read_map supplies canned file
    contents; any path not in the map reads back as an error string.
    """

    def __init__(self, shell_fn, file_read_map: dict[str, str] | None = None):
        self._shell_fn = shell_fn
        self._file_read_map = dict(file_read_map or {})
        self.shell_calls: list[str] = []

    async def execute(self, name, input, on_phase=None):
        if name == "shell":
            cmd = input["command"]
            self.shell_calls.append(cmd)
            return self._shell_fn(cmd)
        if name == "file_read":
            return self._file_read_map.get(input["path"], "Error: file not found")
        if name == "file_write":
            self._file_read_map[input["path"]] = input["content"]
            return "ok"
        if name == "file_edit":
            return "Edited successfully"
        return ""


def _make_context(model_router, tool_executor, task="Implement the login feature"):
    return {
        "task": task,
        "model_router": model_router,
        "tool_executor": tool_executor,
    }


class TestFixLoopSuccess:
    @pytest.mark.asyncio
    async def test_fix_applied_and_verify_passes_stops_loop(self):
        """One failing verify -> LLM emits a REPLACE: fix -> re-run passes -> loop exits."""
        call_count = {"n": 0}

        def shell_fn(cmd):
            if cmd == "npm test":
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return "FAILED: 1 test failed\nTypeError in src/app.js:5"
                return "PASS 3 tests, 0 failures"
            return ""

        tool_executor = _FakeToolExecutor(
            shell_fn, file_read_map={"src/app.js": "console.log('v1');\n// bug\n"}
        )
        router = _FakeModelRouter([
            "FILE: app.js\n```js\nconsole.log('v1')\n```\n\n```shell\nnpm test\n```",
            "REPLACE: src/app.js 1-2\n<<<\nconsole.log('v2 fixed');\n>>>",
        ])
        role = DeveloperRole()
        result = await role.execute(_make_context(router, tool_executor))

        assert result["success"] is True
        assert "src/app.js" in result["files_created"]
        # Verify command ran exactly twice: once failing, once passing.
        assert tool_executor.shell_calls.count("npm test") == 2
        # Loop stopped after the fix succeeded — no cycling/abort message.
        assert "aborted" not in result["response"].lower()


class TestFixLoopCyclingDetection:
    @pytest.mark.asyncio
    async def test_identical_error_twice_aborts_without_exhausting_iterations(self):
        """Same failing output every time -> loop detects cycling and stops early."""

        def shell_fn(cmd):
            if cmd == "npm test":
                return "FAILED: same error every time\nTypeError in src/app.js:5"
            return ""

        tool_executor = _FakeToolExecutor(
            shell_fn, file_read_map={"src/app.js": "console.log('broken');\n"}
        )
        router = _FakeModelRouter([
            "FILE: app.js\n```js\nconsole.log('v1')\n```\n\n```shell\nnpm test\n```",
            "REPLACE: src/app.js 1-1\n<<<\nconsole.log('still broken');\n>>>",
        ])
        role = DeveloperRole()
        result = await role.execute(_make_context(router, tool_executor))

        assert "cycling" in result["response"].lower()
        # Bounded: only ran the initial shell block + one fix-verify cycle,
        # nowhere near MAX_FIX_ITERATIONS (default 50).
        assert tool_executor.shell_calls.count("npm test") == 2


class TestFixLoopNoFileContext:
    @pytest.mark.asyncio
    async def test_no_locatable_source_files_aborts_immediately(self):
        """Failing output with no recognisable source path and no readable
        fallback entry-point file -> loop aborts on the first iteration
        without calling the model again."""

        def shell_fn(cmd):
            if cmd == "npm test":
                return "FAILED: something broke, no file path here"
            return ""

        # No entries in file_read_map -> every fallback entry-point read
        # (package.json, src/main.ts, ...) comes back as "Error: file not found".
        tool_executor = _FakeToolExecutor(shell_fn, file_read_map={})
        router = _FakeModelRouter([
            "```shell\nnpm test\n```",
        ])
        role = DeveloperRole()
        result = await role.execute(_make_context(router, tool_executor))

        assert "aborted" in result["response"].lower()
        assert "could not locate source files" in result["response"].lower()
        # Only the initial generate() call — the fix loop never got far
        # enough to build a fix_prompt and call the model a second time.
        assert len(router.calls) == 1
