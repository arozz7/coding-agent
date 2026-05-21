"""Render code review findings as terminal table, markdown, or JSON."""
from __future__ import annotations

import json
from typing import Optional

from .review_prompts import SEVERITY_EMOJI, CATEGORY_EMOJI, SEVERITY_ORDER


def render_terminal(review: dict, file_path: Optional[str] = None) -> str:
    """Render review as a colored terminal table (emoji-based, no ANSI required)."""
    lines: list[str] = []
    sep = "─" * 60

    # Header
    score = review.get("overall_score", "?")
    score_emoji = _score_emoji(score)
    lines.append(f"\n{score_emoji} Code Review — Score: {score}/10")
    if file_path:
        lines.append(f"📄 {file_path}")
    lines.append(sep)

    # Summary
    summary = review.get("summary", "")
    if summary:
        lines.append(f"\n{summary}")
        lines.append(sep)

    # Findings
    findings = review.get("findings", [])
    if not findings:
        lines.append("\n✅ No issues found.")
        return "\n".join(lines)

    # Group by severity
    by_severity: dict[str, list[dict]] = {}
    for f in findings:
        sev = f.get("severity", "low")
        by_severity.setdefault(sev, []).append(f)

    for sev in sorted(by_severity, key=lambda s: SEVERITY_ORDER.get(s, 99)):
        emoji = SEVERITY_EMOJI.get(sev, "⚪")
        items = by_severity[sev]
        lines.append(f"\n{emoji} [{sev.upper()}] ({len(items)})")

        for f in items:
            cat_emoji = CATEGORY_EMOJI.get(f.get("category", ""), "📌")
            line_num = f.get("line")
            line_str = f" (line {line_num})" if line_num else ""
            title = f.get("title", "")
            message = f.get("message", "")
            suggestion = f.get("suggestion")

            lines.append(f"  {cat_emoji} {title}{line_str}")
            if message:
                lines.append(f"     {message}")
            if suggestion:
                lines.append(f"     💡 {suggestion}")
            lines.append("")

    # Footer
    counts = {sev: len(items) for sev, items in by_severity.items()}
    footer_parts = [f"{SEVERITY_EMOJI.get(s, '')} {s}: {n}" for s, n in sorted(counts.items(), key=lambda x: SEVERITY_ORDER.get(x[0], 99))]
    lines.append(sep)
    lines.append(f"📊 Total: {len(findings)} finding(s) — {', '.join(footer_parts)}")

    return "\n".join(lines)


def render_markdown(review: dict, file_path: Optional[str] = None) -> str:
    """Render review as a markdown document."""
    lines: list[str] = []
    score = review.get("overall_score", "?")
    score_emoji = _score_emoji(score)

    # Header
    lines.append(f"# {score_emoji} Code Review")
    lines.append("")
    if file_path:
        lines.append(f"**File:** `{file_path}`")
    lines.append(f"**Score:** {score}/10")
    lines.append("")

    # Summary
    summary = review.get("summary", "")
    if summary:
        lines.append(f"> {summary}")
        lines.append("")

    # Findings
    findings = review.get("findings", [])
    if not findings:
        lines.append("✅ No issues found.")
        return "\n".join(lines)

    # Group by severity
    by_severity: dict[str, list[dict]] = {}
    for f in findings:
        sev = f.get("severity", "low")
        by_severity.setdefault(sev, []).append(f)

    for sev in sorted(by_severity, key=lambda s: SEVERITY_ORDER.get(s, 99)):
        emoji = SEVERITY_EMOJI.get(sev, "⚪")
        items = by_severity[sev]
        lines.append(f"## {emoji} {sev.upper()} ({len(items)})")
        lines.append("")

        for f in items:
            cat_emoji = CATEGORY_EMOJI.get(f.get("category", ""), "📌")
            line_num = f.get("line")
            line_str = f" (line {line_num})" if line_num else ""
            title = f.get("title", "")
            message = f.get("message", "")
            suggestion = f.get("suggestion")
            snippet = f.get("code_snippet")

            lines.append(f"### {cat_emoji} {title}{line_str}")
            lines.append("")
            if message:
                lines.append(message)
                lines.append("")
            if suggestion:
                lines.append(f"**💡 Suggestion:** {suggestion}")
                lines.append("")
            if snippet:
                lines.append(f"```\n{snippet}\n```")
                lines.append("")

    # Summary table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Severity | Count |")
    lines.append("|----------|-------|")
    for sev in sorted(by_severity, key=lambda s: SEVERITY_ORDER.get(s, 99)):
        emoji = SEVERITY_EMOJI.get(sev, "⚪")
        lines.append(f"| {emoji} {sev.capitalize()} | {len(by_severity[sev])} |")
    lines.append(f"| **Total** | **{len(findings)}** |")

    return "\n".join(lines)


def render_json(review: dict, indent: int = 2) -> str:
    """Render review as formatted JSON."""
    return json.dumps(review, indent=indent, ensure_ascii=False)


def render(review: dict, fmt: str = "terminal", file_path: Optional[str] = None) -> str:
    """Render review in the specified format.

    Args:
        review: Normalized review dict
        fmt: One of 'terminal', 'markdown', 'json'
        file_path: Optional file path for display
    """
    if fmt == "markdown":
        return render_markdown(review, file_path)
    elif fmt == "json":
        return render_json(review)
    else:
        return render_terminal(review, file_path)


def _score_emoji(score: int) -> str:
    """Return an emoji based on review score."""
    if score >= 9:
        return "🟢"
    elif score >= 7:
        return "🟡"
    elif score >= 5:
        return "🟠"
    else:
        return "🔴"
