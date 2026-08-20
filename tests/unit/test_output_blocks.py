"""Regression tests for agent/agents/output_blocks.py block parsing.

Covers the prose-collision bug: when the model's own prose mentions a block
marker word (e.g. "using an APPEND: block") before the real block, an
unbounded DOTALL path capture swallows everything up to the next code fence
— including the real marker line — producing a garbage path. See
logs/api-20260819-184755.log / phase-63 changelog for the live failure this
reproduces (path_traversal_attempt on a mangled APPEND path, sqlx marker
append silently dropped across many prior runs).
"""
from agent.agents.output_blocks import extract_file_appends, extract_file_writes


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
