---
name: wiki-lint
description: Health-check the agent wiki for contradictions, staleness, orphan pages, and missing cross-references
user_invocable: true
---

Health-check the agent wiki for contradictions, staleness, orphan pages, and missing cross-references.

## When to Use
- Periodically during long-running tasks
- When explicitly asked to "check wiki health" or "lint wiki"
- Before starting a major new task (to ensure wiki is reliable)
- After many new compilations (to catch issues)

## Lint Checks

### 1. Cross-reference Symmetry
Check that if A links to B, then B links back to A:
```
# If tech-patterns/physics.md has [[api-usage/physics-api]]
# Then physics-api.md should have [[tech-patterns/physics]]
```

### 2. Staleness Detection
Flag entries that may be outdated:
- Entries with no updates in >30 days
- Entries referencing deprecated APIs
- Entries tagged "superseded" vs "active"

### 3. Orphan Detection
Find pages with no inbound links:
```bash
grep -r "[[page-name]]" .agent-wiki/ | wc -l
# If 0, it's an orphan
```

### 4. Contradiction Detection
Compare claims across entries:
- Entry A says "we use method X"
- Entry B says "we avoid method X"
→ Flag for human review

### 5. Missing Cross-references
Suggest links between related entries:
- Similar tags but no explicit link
- Same feature mentioned in different categories

## Usage

```bash
# Run all checks
wiki-lint

# Run specific check
wiki-lint --check symmetry
wiki-lint --check staleness
wiki-lint --check orphans
```

## Output Format

```markdown
## Wiki Health Report

### Cross-reference Issues
- tech-patterns/physics.md links to api-usage/physics-api but no reverse link

### Stale Entries (not updated >30 days)
- bugs/old-bug.md (last update: 2026-02-15)

### Orphan Pages (no inbound links)
- synthesis/insight-3.md

### Contradictions Found
- decisions/db-choice.md says "we use SQLite"
- decisions/migration-2026.md says "we moved to PostgreSQL"

### Suggested Links
- tech-patterns/auth.md could link to decisions/auth-adr.md
```

## Fix Actions

For each issue, the lint can suggest or perform fixes:
- Add missing cross-references
- Update frontmatter with "superseded" status
- Create links to orphan pages
- Note contradictions for human resolution

## Invocation
```
!ask lint the wiki and fix any issues
!ask check wiki health
```

## Configuration

In `SKILL.md` or `.agent-wiki/config.yaml`:
```yaml
stale_threshold_days: 30
check_symmetry: true
check_orphans: true
check_contradictions: true
auto_fix: false  # require human approval
```