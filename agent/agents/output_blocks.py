"""Output-block parsing and application — FILE:/APPEND:/EDIT:/REPLACE: formats.

Extracted from developer_agent.py: the seven block-format regexes plus the
functions that extract matches from an LLM response and (for APPEND:/
REPLACE:) apply them via a tool_executor. All take their dependencies
(response text, tool_executor, logger) as explicit parameters rather than
reading `self.*`, so they're free functions rather than methods.
"""
from __future__ import annotations

import re
from typing import List

# Fenced shell blocks: ```shell / ```bash / ```sh / ```cmd / ```powershell
SHELL_BLOCK_RE = re.compile(
    r'```(?:shell|bash|sh|cmd|powershell|ps1)\n(.*?)```',
    re.DOTALL | re.IGNORECASE,
)

# Standalone inline backtick commands on their own line, prefixed with a known
# CLI tool.  This catches lines like:  `npm run start`  or  `python app.py`
# but NOT code references like `variable_name`.
INLINE_CMD_RE = re.compile(
    r'^\s*`((?:npm|node|npx|python3?|pip3?|ts-node|cargo|make|cd |git |yarn|pnpm|'
    r'uvicorn|flask|pytest|sh |bash |pwsh|powershell)[^`\n]+)`\s*$',
    re.MULTILINE | re.IGNORECASE,
)

# EDIT: block — surgical multi-hunk patch format
#
#   EDIT: path/to/file.ext
#   <<<OLD
#   exact text to replace
#   ===
#   new replacement text
#   >>>
#
# The regex captures path, old_text, new_text for each hunk.
EDIT_BLOCK_RE = re.compile(
    r'EDIT:\s*(?P<path>[^\n]+)\n'
    r'<<<OLD\n(?P<old_text>.*?)\n===\n(?P<new_text>.*?)\n>>>',
    re.DOTALL,
)

# REPLACE: block — line-number-based patch (no old-text matching required).
#
#   REPLACE: path/to/file.ext 45-47
#   <<<
#   replacement line 1
#   replacement line 2
#   >>>
#
# Line numbers are 1-indexed and inclusive.  The system reads the current
# file, splices in the new lines, and writes the result back — eliminating
# the "old text not found" failure mode of EDIT: blocks.
REPLACE_BLOCK_RE = re.compile(
    r'REPLACE:\s*(?P<path>\S+)\s+(?P<start>\d+)-(?P<end>\d+)\n'
    r'<<<\n(?P<new_text>.*?)\n>>>',
    re.DOTALL,
)

# APPEND: block — appends content to the END of an existing file without overwriting.
#
#   APPEND: path/to/file.ext
#   ```language
#   content to add at the end
#   ```
#
# The path group is [^\n]+ (not .+?) so it can never cross a newline even
# under DOTALL. Without that restriction, when the model's own prose
# mentions "APPEND:" before the real block (e.g. "I'll append the marker
# using an APPEND: block:\n\nAPPEND: real/path.sql\n```sql\n..."), the
# non-greedy .+? would swallow everything between the prose mention and the
# next code fence — including the real "APPEND: real/path.sql" line — into
# the path, producing garbage like "block:\n\nAPPEND: real/path.sql" that
# fails the path-traversal guard and silently drops the append. Restricting
# the path to a single line makes that prose match fail outright (no fence
# immediately follows it), so the regex backtracks to the real marker line.
APPEND_BLOCK_RE = re.compile(
    r'APPEND:\s*([^\n]+)\n```\w*\n(.*?)```',
    re.DOTALL,
)

# SKILL: block — agent-proposed reusable fix recipe (saved to .agent-wiki/skills/)
SKILL_BLOCK_RE = re.compile(
    r"SKILL:\s*(?P<title>[^\n]+)\n(?P<body>.*?)(?=\nSKILL:|\nFILE:|\nEDIT:|\nREPLACE:|\Z)",
    re.DOTALL,
)

# SCRIPT: block — agent-created helper script (saved to scripts/)
SCRIPT_BLOCK_RE = re.compile(
    r"SCRIPT:\s*(?P<name>\S+)\n```(?:\w+)?\n(?P<content>.*?)```",
    re.DOTALL,
)


def format_file_with_lines(content: str, path: str, max_chars: int = 3000) -> str:
    """Return a numbered-line view of *content* suitable for anchor-and-patch prompts."""
    lines = content.splitlines()
    numbered = "\n".join(f"{i + 1:4}: {line}" for i, line in enumerate(lines))
    header = f"=== {path} ({len(lines)} lines) ==="
    full = header + "\n" + numbered
    if len(full) > max_chars:
        truncated = full[:max_chars]
        cut = truncated.rfind("\n")
        shown = truncated[:cut].count("\n") + 1
        full = full[:cut] + f"\n   … ({len(lines) - shown} more lines omitted)"
    return full


def extract_file_writes(response: str) -> List[tuple]:
    # Path group is [^\n]+ (not .+?) — see APPEND_BLOCK_RE comment above for
    # why an unbounded, DOTALL-crossing path group misfires when the model's
    # prose mentions "FILE:" before the real block.
    pattern = r'FILE:\s*([^\n]+)\n```\w*\n(.*?)```'
    matches = re.findall(pattern, response, re.DOTALL)
    return [(path.strip(), content.strip()) for path, content in matches]


def extract_file_appends(response: str) -> List[tuple]:
    """Extract APPEND: blocks — content to add at the end of existing files."""
    matches = APPEND_BLOCK_RE.findall(response)
    return [(path.strip(), content.strip()) for path, content in matches]


async def execute_append(
    file_path: str,
    content: str,
    files_created: List[str],
    tool_executor,
    logger,
) -> None:
    """Read the existing file (if any) and write back with content appended."""
    try:
        existing = await tool_executor.execute("file_read", {"path": file_path})
        base = "" if existing.startswith("Error") else existing
        combined = base.rstrip("\n") + "\n" + content if base else content
        await tool_executor.execute("file_write", {"path": file_path, "content": combined})
        if file_path not in files_created:
            files_created.append(file_path)
        logger.info("file_appended", path=file_path, added_bytes=len(content))
    except Exception as e:
        logger.error("file_append_failed", path=file_path, error=str(e))


def extract_file_edits(response: str) -> List[tuple]:
    """Extract EDIT: blocks → [(path, old_text, new_text), ...]."""
    results = []
    for m in EDIT_BLOCK_RE.finditer(response):
        path = m.group("path").strip()
        old_text = m.group("old_text")
        new_text = m.group("new_text")
        results.append((path, old_text, new_text))
    return results


def extract_line_replacements(response: str) -> List[tuple]:
    """Extract REPLACE: blocks → [(path, start, end, new_text), ...]."""
    results = []
    for m in REPLACE_BLOCK_RE.finditer(response):
        path = m.group("path").strip()
        start = int(m.group("start"))
        end = int(m.group("end"))
        new_text = m.group("new_text")
        results.append((path, start, end, new_text))
    return results


async def apply_line_replacement(
    tool_executor, path: str, start: int, end: int, new_text: str, logger
) -> bool:
    """Splice new_text into *path* at 1-indexed lines start–end (inclusive).

    Reads the current file, replaces the line range, writes back.
    Returns True on success, False if read or write fails.
    """
    content = await tool_executor.execute("file_read", {"path": path})
    if content.startswith("Error"):
        logger.warning("replace_read_failed", path=path)
        return False
    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if not line.endswith("\n"):
            lines[i] = line + "\n"
    s = max(0, start - 1)
    e = min(len(lines), end)
    replacement = new_text.splitlines(keepends=True)
    for i, line in enumerate(replacement):
        if not line.endswith("\n"):
            replacement[i] = line + "\n"
    updated = "".join(lines[:s] + replacement + lines[e:])
    result = await tool_executor.execute("file_write", {"path": path, "content": updated})
    return not (isinstance(result, str) and result.startswith("Error"))
