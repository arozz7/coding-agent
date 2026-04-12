# AGENTS.md — Global Coding Agent Instructions

This file is automatically injected into every agent's context at the start of each task.
It defines global standards, conventions, and behaviors that all agents must follow.

## Core Philosophy

> Readability and Order > Speed and Complex Interconnections

- **SOLID Principles:** Single Responsibility, Open-Closed, Liskov Substitution, Interface Segregation, Dependency Inversion
- **DRY:** Extract common logic into reusable functions, modules, or classes
- **KISS:** Avoid over-engineering; the simplest solution that works is usually the best
- **Clean Code:** Meaningful names, small focused functions, clear structure, self-documenting code

## The Golden Rule (CRITICAL)

**DO NOT write code files** until the user has explicitly approved an implementation plan for any multi-file or architectural change.

For simple single-file or one-function changes: proceed directly.
For new projects, new features, or refactors spanning multiple files: ask the user to use `!ask plan first: ...` to get a plan reviewed before building.

## Project Directory Rule (CRITICAL)

When building a NEW project (game, app, API, service, tool, bot, etc.):
1. Infer a short, lowercase, hyphenated project name from the task
2. Create ALL project files under `<project-name>/` as the top-level directory
3. NEVER place new project files directly in the workspace root

Examples:
```
Task: "Build an RPG game"         → rpg-game/
Task: "Create a REST API"         → rest-api/
Task: "Make a Discord bot"        → discord-bot/
Task: "Write a CLI tool"          → cli-tool/
```

When continuing an existing project, always use the same project directory.

## Plan / Build Workflow

If the user says "plan first", "I want to plan", "solid plan", or similar:
1. The **Plan Agent** produces a markdown plan — no code, no files
2. The user reviews and approves
3. The user then says "build it" / "proceed" / "looks good" to start implementation
4. The **Developer Agent** builds against the approved plan (available in conversation history)

## File Size Limits

- Soft limit: 300 lines — consider splitting
- Hard limit: 600 lines — must split using refactoring protocol

## Error Handling

Use structured logging throughout. Never swallow exceptions silently.

## Testing

- Write unit tests for new functions and classes
- Include basic integration tests for new APIs and services
- Place tests in a `tests/` subdirectory within the project directory

## Security

- Never hard-code secrets, API keys, or passwords — use environment variables
- Validate all user/external inputs at system boundaries
- Use parameterized queries for any database operations

## Shell & Terminal (Windows)

- Shell scripts use PowerShell (`.ps1` extension)
- Use forward slashes in paths where possible
- Do not use `sudo`; use "Run as Administrator" context if needed

## Agent Wiki

The `.agent-wiki/` directory contains the agent's accumulated knowledge base.
- **wiki-query** runs automatically before every task to surface relevant prior knowledge
- **wiki-compile** runs automatically after every successful task to capture new learnings
- Agents should check wiki context before searching from scratch
