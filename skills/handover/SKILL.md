---
name: handover
description: Generate a Context Bridge document that captures conversation state for seamless handoff to a future AI session
user_invocable: true
---

Generate a concise "Context Bridge" document that captures the full state of the current conversation so a future AI session can pick up exactly where we left off.

## Instructions

1. **Review the conversation history** thoroughly — identify all objectives discussed, decisions made, technical constraints established, and work completed.

2. **Check project memory** — read any relevant memory files in the project memory directory for persistent context about the project and user.

3. **Check recent git history** — run `git log --oneline -10` and `git status` to capture the current state of the codebase.

4. **Output the Context Bridge** in the following format:

---

### Current State
_A 3-sentence summary of the key objectives discussed and the decisions made so far._

### Technical Details
_A bulleted list of specific constraints, data points, or preferences established in this chat._

### Next Steps
_A clear, prioritized list of what the next AI should focus on to move this project forward._

### Opening Instruction
_Write a single sentence that the user can paste into a new chat to instantly prime the next AI with this context._

---

## Rules

- Be concise but comprehensive — capture enough detail that no context is lost.
- Include specific file paths, function names, and config values where relevant.
- Prioritize actionable information over background narrative.
- The Opening Instruction should be self-contained and reference the project by name.
- Do NOT create any files — output the Context Bridge directly in the chat.
