# User Manual: Local Coding Agent

A comprehensive guide to using the Local Coding Agent.

## Table of Contents

1. [Getting Started](#getting-started)
2. [Configuration](#configuration)
3. [Basic Usage](#basic-usage)
4. [Interactive Mode](#interactive-mode)
5. [Session Management](#session-management)
6. [Tools Reference](#tools-reference)
7. [Troubleshooting](#troubleshooting)

---

## Getting Started

### System Requirements

- Python 3.11 or higher
- Windows, macOS, or Linux
- Local LLM server (Ollama) or OpenAI-compatible API

### Installation Steps

1. **Clone or navigate to the project:**
   ```powershell
   cd J:\Projects\coding-agent
   ```

2. **Install Python dependencies:**
   ```powershell
   C:\Users\arozz\AppData\Local\Programs\Python\Python313\python.exe -m pip install pydantic pydantic-settings langgraph httpx structlog chromadb
   ```

3. **Start your LLM server:**
   ```powershell
   # If using Ollama
   ollama serve
   
   # Or if using LM Studio or similar
   # Ensure it's running at http://127.0.0.1:1234
   ```

4. **Verify installation:**
   ```bash
   python -m local_coding_agent --task "Say hello"
   ```

---

## Configuration

### Model Configuration

Edit `config/models.yaml` to set up your models:

```yaml
models:
  - name: qwen3.5-35b-a3b
    type: local                    # "local" or "remote"
    endpoint: http://127.0.0.1:1234  # API endpoint
    context_window: 262144         # Maximum context size
    is_coding_optimized: true      # Mark as coding model
    recommended_for:
      - coding
      - code_review
      - test_generation
    rate_limit_rpm: 120          # Requests per minute
```

### Supported Model Types

| Type | Description | Example |
|------|-------------|---------|
| `local` | Ollama or OpenAI-compatible API | `qwen3.5-35b-a3b` |
| `remote` | Cloud API (Anthropic, OpenAI) | `claude-3-5-sonnet` |

### Remote API Configuration

For cloud APIs, add your API key via environment:

```powershell
$env:ANTHROPIC_API_KEY = "your-key-here"
$env:OPENAI_API_KEY = "your-key-here"
```

---

## Basic Usage

### Single Task Mode

Run a single task and exit:

```bash
python -m local_coding_agent --task "Write a Python function to calculate fibonacci"
```

### With Workspace Directory

Specify a workspace for file operations:

```bash
python -m local_coding_agent --workspace ./my-project --task "Create a README"
```

### Multiple Arguments

```bash
python -m local_coding_agent \
    --workspace ./my-project \
    --config config/models.yaml \
    --task "Add error handling to main.py"
```

---

## Interactive Mode

Start an interactive session:

```bash
python -m local_coding_agent
```

### Available Commands

| Command | Description |
|---------|-------------|
| `exit` / `quit` | Exit interactive mode |
| `history` | Show current conversation history |
| `sessions` | List all saved sessions |
| `resume <id>` | Switch to a different session |

### Example Session

```
> Write a hello world script

--- Response ---

# Hello World in Python

```python
print("Hello, World!")
```

> Add a function

--- Response ---

# Updated Script

```python
def greet(name):
    return f"Hello, {name}!"

print(greet("World"))
```

> exit
```

---

## Session Management

### Why Sessions Matter

Sessions allow the agent to maintain context across multiple requests. The agent remembers previous conversations and can build upon them.

### List All Sessions

```bash
python -m local_coding_agent --list-sessions
```

Output:
```
Sessions:
------------------------------------------------------------
session_20260410_123456            5 msgs  active     2026-04-10
session_20260410_111222            3 msgs  active     2026-04-10
```

### Resume a Session

```bash
python -m local_coding_agent --session session_20260410_123456
```

### Disable History Context

If you don't want the agent to consider previous messages:

```bash
python -m local_coding_agent --no-history --task "New topic"
```

---

## Tools Reference

### File System Tools

#### read_file
Read contents of a file.

```bash
# The agent automatically uses this when you ask to read files
> Read the main.py file
```

#### write_file
Write content to files.

```bash
# The agent automatically uses this when you ask to create/modify files
> Create a new file called config.json
```

#### list_directory
List contents of a directory.

```bash
# Automatic when exploring project structure
> What files are in this project?
```

#### search_files
Find files matching a pattern.

```bash
# Automatic for file discovery
> Find all Python files in the project
```

### Git Tools

#### git_status
Show working tree status.

```bash
# Automatic when checking repository state
> What changes have I made?
```

#### git_diff
Show unstaged changes.

```bash
# Automatic for viewing modifications
> Show me what changed
```

#### git_diff_staged
Show staged changes.

```bash
# Automatic for staged changes
> What's ready to commit?
```

#### git_commit
Commit staged changes.

```bash
# When committing
> Commit my changes with message "Added new feature"
```

#### git_log
Show commit history.

```bash
# Automatic for history queries
> Show recent commits
```

#### git_branch
List branches.

```bash
# When working with branches
> What branches exist?
```

---

## Troubleshooting

### Common Issues

#### "No model configured"

**Solution:** Check `config/models.yaml` exists and has valid configuration.

#### "Connection refused" / API errors

**Solution:** 
1. Verify your LLM server is running
2. Check the endpoint URL in `config/models.yaml`
3. Test with curl:
   ```bash
   curl http://127.0.0.1:1234/v1/models
   ```

#### "Rate limit exceeded"

**Solution:** Wait a moment, or adjust `rate_limit_rpm` in config.

#### "Session not found"

**Solution:** Use `--list-sessions` to see available sessions.

### Verbose Mode

Enable detailed logging for debugging:

```bash
python -m local_coding_agent --task "..." --verbose
```

### Reset Data

To start fresh:

```bash
# Delete session database
Remove-Item data/memory.db

# Or create a new database
New-Item -ItemType File -Path data/memory.db
```

---

## Advanced Configuration

### Cost Tracking

The agent tracks token usage and estimated costs for cloud APIs:

```yaml
models:
  - name: claude-3-5-sonnet
    cost_per_1k_input: 0.003
    cost_per_1k_output: 0.015
```

### Rate Limiting

Configure per-model rate limits:

```yaml
models:
  - name: model-name
    rate_limit_rpm: 60  # Requests per minute
```

### Health Checks

The agent monitors model availability and will skip unhealthy models.

---

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+C` | Interrupt current operation |
| `Ctrl+D` | Exit (in some terminals) |

---

## Getting Help

For issues or feature requests, check:
- Project repository issues
- Documentation in `docs/`

---

*Last updated: 2026-04-10*
