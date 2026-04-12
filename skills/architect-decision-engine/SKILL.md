---
name: architect-adr
description: Activates when a task involves core system changes (DB, API, Auth).
---
# Architectural Decision Engine
1. **Analyze Impact:** Run `python ./scripts/check_adr_impact.py "{{user_prompt}}"`.
2. **Draft ADR:** If `requires_adr` is true, create a new file in `/docs/adr/XXXX-title.md` using the Nygard template (Context, Decision, Consequences).
3. **Wait for Approval:** Do NOT write implementation code until the user approves the ADR artifact.