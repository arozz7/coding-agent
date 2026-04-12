---
name: wiki-query
description: Query the persistent wiki knowledge base before answering questions
user_invocable: true
---

Query the persistent wiki knowledge base before answering questions or starting new tasks.

## When to Use
- When asked a question about the codebase
- Before starting a new task that might relate to past work
- When the user mentions something the agent might have learned before
- Any time the agent would otherwise search for information from scratch

## How It Works

### Query Flow
1. Extract key terms from the question/task
2. Search wiki index (`index.md`) for relevant entries
3. Read matching wiki pages
4. Synthesize answer using wiki knowledge + current context

### Search Strategy
1. **Index-first**: Always check `index.md` for category overview
2. **Tag search**: Look for entries with matching tags
3. **Keyword search**: Search content for relevant terms
4. **Link traversal**: Follow wikilinks to related entries

## Usage

```
User: "How did we fix the screenshot bug?"
→ Query wiki for "screenshot bug"
→ Find: .agent-wiki/bugs/screenshot-timeout.md
→ Read and respond with compiled knowledge

User: "What's our approach for game physics?"
→ Query wiki for "physics"
→ Find: .agent-wiki/tech-patterns/physics-implementation.md
→ Read and respond
```

## Wiki Structure

```
.agent-wiki/
├── index.md          # All entries catalog
├── log.md            # Chronological compilation log
├── tech-patterns/    # Technical patterns
├── bugs/             # Bug fixes and workarounds
├── decisions/        # Architectural decisions
├── api-usage/        # API usage patterns
└── synthesis/        # Cross-domain insights
```

## Query Examples

### Direct lookup
```python
# Search index for "screenshot"
with open(".agent-wiki/index.md") as f:
    content = f.read()
    if "screenshot" in content.lower():
        # Found mention, read relevant file
```

### Tag-based search
```python
# Find all entries with tag "bug-fix"
for f in Path(".agent-wiki").rglob("*.md"):
    if frontmatter.get("tags") and "bug-fix" in frontmatter["tags"]:
        results.append(f)
```

### Link-based traversal
```python
# Follow wikilinks from known entry
with open(known_file) as f:
    for line in f:
        if line.startswith("[["):
            linked_file = extract_link(line)
            # Read and include in context
```

## Output Format

When wiki provides useful information:

```markdown
**From Agent Wiki:**
- [entry-name](.agent-wiki/path/to/entry.md)
- Summary: <extracted summary>
- Last updated: <timestamp>
```

When wiki has no relevant info:

```markdown
No relevant entries in agent wiki. Will search current context.
```

## Key Principle
The wiki is a persistent, compounding artifact. Cross-references are already there.
The agent should check wiki BEFORE searching or deriving knowledge from scratch.

## Invocation
This skill runs automatically on every query:
1. Parse the query for key terms
2. Check wiki index for category/tags
3. Read relevant entries
4. Include wiki context in prompt to LLM