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
