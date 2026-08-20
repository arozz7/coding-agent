"""Regression tests for agent/agents/output_blocks.py block parsing.

Covers the prose-collision bug: when the model's own prose mentions a block
marker word (e.g. "using an APPEND: block") before the real block, an
unbounded DOTALL path capture swallows everything up to the next code fence
— including the real marker line — producing a garbage path. See
logs/api-20260819-184755.log / phase-63 changelog for the live failure this
reproduces (path_traversal_attempt on a mangled APPEND path, sqlx marker
append silently dropped across many prior runs).
"""
from agent.agents.output_blocks import (
    extract_file_appends,
    extract_file_writes,
    is_powershell_script,
    split_shell_block,
)


def test_extract_file_appends_ignores_prose_mention_of_append():
    response = (
        "I've read the migration file. Now I'll append the required "
        "`-- sqlx-migrate` marker using an APPEND: block (not FILE:):\n\n"
        "APPEND: src-tauri/db/migrations/20260622000000_initial.sql\n"
        "```sql\n-- sqlx-migrate\n```\n"
    )
    matches = extract_file_appends(response)
    assert matches == [
        ("src-tauri/db/migrations/20260622000000_initial.sql", "-- sqlx-migrate")
    ]


def test_extract_file_appends_simple_case_still_works():
    response = "APPEND: notes.txt\n```text\nhello\n```\n"
    assert extract_file_appends(response) == [("notes.txt", "hello")]


def test_extract_file_appends_multiple_real_blocks():
    response = (
        "APPEND: a.txt\n```text\nfirst\n```\n\n"
        "APPEND: b.txt\n```text\nsecond\n```\n"
    )
    assert extract_file_appends(response) == [
        ("a.txt", "first"),
        ("b.txt", "second"),
    ]


def test_extract_file_writes_ignores_prose_mention_of_file():
    response = (
        "I'll create this as a new FILE: rather than an EDIT.\n\n"
        "FILE: src/new_module.py\n"
        "```python\nprint('hi')\n```\n"
    )
    matches = extract_file_writes(response)
    assert matches == [("src/new_module.py", "print('hi')")]


def test_extract_file_writes_simple_case_still_works():
    response = "FILE: app.py\n```python\nprint('hi')\n```\n"
    assert extract_file_writes(response) == [("app.py", "print('hi')")]


# --- is_powershell_script / split_shell_block -------------------------------
# Regression coverage for logs/api-20260819-201238.log: a PowerShell block
# assigning $root/$log/$err and calling Start-Process was split naively by
# newline into per-line "commands", each failing with WinError 2 because a
# bare `$root = "..."` line isn't an executable.


def test_is_powershell_script_detects_explicit_fence_language():
    assert is_powershell_script("powershell", "Write-Host 'hi'") is True
    assert is_powershell_script("ps1", "Write-Host 'hi'") is True


def test_is_powershell_script_detects_variable_assignment_in_generic_fence():
    content = (
        '$root = "J:\\Projects\\agent-workspace\\payment-tracker"\n'
        '$log  = "$root\\tauri-launch.log"\n'
        'Start-Process -FilePath "cargo" -ArgumentList "run"\n'
    )
    assert is_powershell_script("shell", content) is True


def test_is_powershell_script_false_for_plain_command_sequence():
    assert is_powershell_script("shell", "npm install\nnpm run build\n") is False


def test_split_shell_block_plain_commands_unchanged():
    content = "npm install\nnpm run build\n"
    assert split_shell_block(content) == ["npm install", "npm run build"]


def test_split_shell_block_keeps_multiline_quoted_argument_together():
    # A python -c "..." heredoc-style call must stay one command — splitting
    # on every newline breaks the quoted script argument apart mid-string.
    content = 'python -c "\ncontent = f.read()\nfor line in content:\n    pass\n"\n'
    result = split_shell_block(content)
    assert result == [
        'python -c "\ncontent = f.read()\nfor line in content:\n    pass\n"'
    ]


def test_split_shell_block_ignores_comment_lines():
    content = "# a comment\nnpm install\n"
    assert split_shell_block(content) == ["npm install"]
