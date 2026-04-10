---
name: wiki-compile
description: Compile learned patterns from agent sessions into a persistent wiki knowledge base
user_invocable: true
---

Compile learned patterns from agent sessions into a persistent wiki. This enables the agent to build a knowledge base that compounds over time.

## When to Use
- After completing a significant task or discovering a pattern
- When asked to "save this knowledge" or "remember this"
- After fixing a bug or implementing a feature
- When the agent learns something that would be useful for future tasks

## How It Works

### Raw Layer (Immutable)
- Source files in the workspace
- Agent conversation history (in SessionMemory)
- Code artifacts and documentation

### Wiki Layer (Agent-maintained)
The agent writes markdown files to `.agent-wiki/`:
- `tech-patterns/` - Technical patterns discovered in codebase
- `bugs/` - Bug fixes and workarounds found
- `decisions/` - Architectural decisions made
- `api-usage/` - How the codebase uses external APIs
- `index.md` - Catalog of all wiki entries
- `log.md` - Chronological record of compilations

### Schema (CLAUDE.md)
The agent wiki schema is defined in AGENTS.md or CLAUDE.md

## Usage

```
After completing task: "Convert game from RPG to shooter"
→ Write: .agent-wiki/decisions/game-genre-change.md
→ Update: .agent-wiki/index.md
→ Append: .agent-wiki/log.md
```

## Output Format

Each wiki entry should have:

```markdown
---
title: <descriptive title>
tags: [<category>, <feature>]
created: <timestamp>
confidence: high|medium|speculative
---

## Summary
<2-3 sentence summary>

## Key Details
- Point 1
- Point 2

## Connections
- [[related-entry]] - relation description

## Contradictions
- Any known conflicts with existing wiki entries
```

## Files Created
- `.agent-wiki/tech-patterns/<pattern>.md`
- `.agent-wiki/bugs/<bug>.md`
- `.agent-wiki/decisions/<decision>.md`
- `.agent-wiki/api-usage/<api>.md`
- `.agent-wiki/index.md` - auto-updated
- `.agent-wiki/log.md` - auto-appended

## Invocation
This skill is invoked automatically after:
1. A task completes successfully
2. User asks to "save" or "remember" something
3. Agent discovers a reusable pattern

The agent should proactively offer to compile knowledge when it discovers something worth preserving.