"""Unit tests for ShellTool's Windows shell-operator detection.

Compound commands (`cargo check ... 2>&1 || true`, `node -c x.js & echo
__EXIT__%ERRORLEVEL%`) must run with shell=True on Windows even when the
leading token resolves to a real .exe rather than a cmd.exe builtin or
.cmd/.bat script — otherwise the operators are passed as literal argv to
the executable, producing a bogus failure on every invocation.
"""
import pytest

from agent.tools.shell_tool import ShellTool, _has_unquoted_shell_operators


@pytest.mark.parametrize("cmd,expected", [
    ("cargo check --manifest-path src-tauri/Cargo.toml 2>&1 || true", True),
    ("node -c index.js & echo __EXIT__%ERRORLEVEL%", True),
    ("git log | findstr foo", True),
    ("python script.py > out.txt", True),
    ("cargo check --manifest-path src-tauri/Cargo.toml", False),
    ('echo "a & b"', False),  # operator inside quotes doesn't count
])
def test_has_unquoted_shell_operators(cmd, expected):
    assert _has_unquoted_shell_operators(cmd) is expected


def test_resolve_args_routes_compound_exe_command_through_shell(monkeypatch):
    monkeypatch.setattr("agent.tools.shell_tool.IS_WINDOWS", True)
    tool = object.__new__(ShellTool)
    cmd = "cargo check --manifest-path src-tauri/Cargo.toml 2>&1 || true"
    args, use_shell = tool._resolve_args(cmd)
    assert use_shell is True
    assert args == cmd


def test_resolve_args_simple_exe_command_uses_argv(monkeypatch):
    monkeypatch.setattr("agent.tools.shell_tool.IS_WINDOWS", True)
    monkeypatch.setattr("shutil.which", lambda *a, **k: "C:\\fake\\cargo.exe")
    tool = object.__new__(ShellTool)
    cmd = "cargo check --manifest-path src-tauri/Cargo.toml"
    args, use_shell = tool._resolve_args(cmd)
    assert use_shell is False
    assert args[0] == "cargo"


class TestTranslateFindToWindows:
    """`find <dir> -type f -name '<pattern>'` is a bare Unix filesystem
    search the agent may issue directly (not just via a criterion — see
    normalize_criterion in criterion_evaluator.py for the criteria-level
    fix). On Windows this must become a `dir` recursive listing rather than
    silently invoking cmd.exe's built-in `find` (which searches file
    *contents*, not paths, and always fails or misbehaves here).
    """

    def _translate(self, cmd: str) -> str:
        tool = object.__new__(ShellTool)
        return tool._translate_unix_to_windows(cmd)

    def test_find_type_f_name_translated_to_dir_recursive(self):
        result = self._translate("find src-tauri/db/ -type f -name *.sql")
        assert result == 'dir /s /b "src-tauri\\db\\*.sql"'

    def test_find_current_dir(self):
        result = self._translate("find . -type f -name *.py")
        assert result == 'dir /s /b "*.py"'

    def test_find_without_type_f_still_translated(self):
        result = self._translate("find src -name *.rs")
        assert result == 'dir /s /b "src\\*.rs"'

    def test_find_with_no_name_falls_through_unchanged(self):
        # No recognizable pattern to translate — leave it for the caller to
        # fail loudly rather than guess.
        result = self._translate("find src -type f")
        assert result == "find src -type f"
