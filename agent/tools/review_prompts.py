"""Language-specific code review prompt templates with JSON schema enforcement."""
from __future__ import annotations

import json
from typing import Optional

# JSON schema the LLM must return for reliable parsing
REVIEW_JSON_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "overall_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["bug", "security", "performance", "style", "design", "testing", "documentation"]
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low", "info"]
                    },
                    "line": {"type": ["integer", "null"]},
                    "file": {"type": ["string", "null"]},
                    "title": {"type": "string"},
                    "message": {"type": "string"},
                    "suggestion": {"type": ["string", "null"]},
                    "code_snippet": {"type": ["string", "null"]}
                },
                "required": ["category", "severity", "title", "message"]
            }
        }
    },
    "required": ["overall_score", "summary", "findings"]
}, indent=2)

# Per-language best-practice context injected into system prompt
LANGUAGE_GUIDELINES: dict[str, str] = {
    "python": """
Python-specific checks:
- PEP 8 compliance (naming, spacing, line length)
- Type hints on public functions
- Use of context managers for resources
- Avoid bare except clauses
- Use f-strings over format/concatenation
- Check for mutable default arguments
- Prefer pathlib over os.path
- Use dataclasses / TypedDict where appropriate
""",
    "javascript": """
JavaScript-specific checks:
- Prefer const/let over var
- Check for proper error handling (try/catch, async/await)
- Avoid direct DOM manipulation in frameworks
- Use proper module exports/imports
- Check for potential closure issues
- Validate null/undefined handling
- Prefer arrow functions for callbacks
- Check for unused variables/imports
""",
    "typescript": """
TypeScript-specific checks:
- Strict null checks enabled
- Avoid 'any' type usage
- Proper generic type parameters
- Interface vs type alias usage
- Check for proper type guards
- Use readonly where applicable
- Prefer interfaces for object shapes
- Check for unused imports
""",
    "go": """
Go-specific checks:
- Error handling (check all returned errors)
- Proper use of interfaces (accept interfaces, return structs)
- Context propagation in function chains
- Avoid goroutine leaks
- Check for race conditions
- Proper use of sync primitives
- Error wrapping with fmt.Errorf("%w")
- Interface satisfaction checks
""",
    "rust": """
Rust-specific checks:
- Proper use of Result/Option
- Avoid unwrap() in production code
- Check for unnecessary cloning
- Proper lifetime annotations
- Use of ? operator for error propagation
- Check for unsafe blocks
- Proper use of Arc/Mutex vs Rc/RefCell
- Clippy lint compliance
""",
    "java": """
Java-specific checks:
- Check for proper exception handling
- Avoid raw types, use generics
- Check for resource leaks (use try-with-resources)
- Proper use of final/finalize
- Check for N+1 query patterns
- Use records for data classes (Java 16+)
- Avoid checked exceptions for control flow
- Check for proper equals/hashCode implementation
""",
    "c": """
C-specific checks:
- Check for buffer overflows (strlen, strcpy)
- Proper memory management (malloc/free pairing)
- Check for uninitialized variables
- Use of const for read-only parameters
- Check for integer overflow
- Proper error return values
- Avoid variable-length arrays
- Check for format string vulnerabilities
""",
    "cpp": """
C++-specific checks:
- Prefer smart pointers over raw pointers
- Check for Rule of Three/Five compliance
- Use const-correctness
- Avoid memory leaks
- Check for proper move semantics
- Prefer std::string_view over const std::string&
- Check for exception safety
- Avoid #include of implementation headers
""",
    "bash": """
Bash-specific checks:
- Use set -euo pipefail
- Quote all variable expansions
- Check for word splitting issues
- Use [[ ]] instead of [ ]
- Check for proper exit codes
- Avoid parsing ls output
- Use local variables in functions
- Check for glob expansion issues
""",
    "sql": """
SQL-specific checks:
- Use parameterized queries (no string concatenation)
- Check for missing indexes on WHERE/JOIN columns
- Avoid SELECT *
- Check for N+1 query patterns
- Use EXISTS instead of IN for subqueries
- Check for proper transaction usage
- Avoid functions on indexed columns in WHERE
- Check for proper NULL handling
""",
}


def get_system_prompt(language: Optional[str] = None) -> str:
    """Build the system prompt for code review with language-specific guidelines."""
    guidelines = LANGUAGE_GUIDELINES.get(language or "", "")
    language_note = f"The code is written in **{language}**." if language else ""

    return f"""You are an expert code reviewer. Analyze the provided code and return a structured review.

{language_note}
{guidelines}

## Output Format

You MUST return valid JSON matching this schema (no markdown code fences, no prose outside the JSON):

{REVIEW_JSON_SCHEMA}

## Severity Levels

- **critical**: Will cause data loss, security breach, or system crash
- **high**: Significant bug or vulnerability that will cause issues in production
- **medium**: Code quality issue that should be fixed
- **low**: Style or minor improvement
- **info**: Informational suggestion

## Categories

- **bug**: Logic errors, edge cases, incorrect behavior
- **security**: Vulnerabilities, injection, auth issues
- **performance**: Inefficiency, memory leaks, slow queries
- **style**: Naming, formatting, consistency
- **design**: Architecture, patterns, coupling
- **testing**: Missing tests, test quality
- **documentation**: Missing docs, unclear comments

## Rules

1. Only report findings with real substance — no nitpicking whitespace
2. Include line numbers when possible
3. Provide actionable suggestions, not just criticism
4. Score honestly: 10 = production-ready, 7 = good with minor issues, 5 = needs work, <5 = significant problems
5. If the code is short or incomplete, note that in the summary
6. Return ONLY the JSON object, nothing else"""


def get_user_prompt(code: str, file_path: Optional[str] = None, context: Optional[str] = None) -> str:
    """Build the user prompt with code and optional context."""
    parts = []

    if file_path:
        parts.append(f"File: `{file_path}`")

    if context:
        parts.append(f"Additional context:\n{context}")

    parts.append(f"Code to review:\n```\n{code}\n```")

    return "\n\n".join(parts)


def get_diff_review_prompt(diff: str, file_path: Optional[str] = None) -> tuple[str, str]:
    """Build prompts specifically for reviewing a git diff."""
    language = None
    if file_path:
        from .code_chunker import get_language_from_extension
        language = get_language_from_extension(file_path)

    system = get_system_prompt(language)
    system += """

This is a **git diff review**. Focus on the *changed lines only*.
Ignore unchanged code. Note if a change introduces a regression.
"""

    user = f"""Git diff to review:
```diff
{diff}
```
"""
    if file_path:
        user = f"File: `{file_path}`\n\n" + user

    return system, user


def parse_review_response(raw: str) -> Optional[dict]:
    """Parse LLM response into structured review dict.

    Handles:
    - Pure JSON
    - JSON wrapped in markdown code fences
    - Partial JSON with surrounding prose
    """
    # Strip markdown code fences if present
    text = raw.strip()
    if text.startswith("```"):
        # Remove opening fence (possibly with language tag)
        text = text.split("\n", 1)[1] if "\n" in text else ""
        # Remove closing fence
        if text.endswith("```"):
            text = text[:-3].strip()

    # Try direct parse
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the text
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        json_candidate = text[brace_start:brace_end + 1]
        try:
            result = json.loads(json_candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    return None


def normalize_review(raw: dict) -> dict:
    """Normalize a parsed review dict, ensuring required fields exist."""
    return {
        "overall_score": int(raw.get("overall_score", 5)),
        "summary": raw.get("summary", "No summary provided."),
        "findings": [
            {
                "category": f.get("category", "style"),
                "severity": f.get("severity", "low"),
                "line": f.get("line"),
                "file": f.get("file"),
                "title": f.get("title", "Untitled"),
                "message": f.get("message", ""),
                "suggestion": f.get("suggestion"),
                "code_snippet": f.get("code_snippet"),
            }
            for f in raw.get("findings", [])
            if isinstance(f, dict)
        ],
    }


# Severity display mapping
SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
    "info": "⚪",
}

CATEGORY_EMOJI = {
    "bug": "🐛",
    "security": "🔒",
    "performance": "⚡",
    "style": "🎨",
    "design": "🏗️",
    "testing": "🧪",
    "documentation": "📝",
}

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
