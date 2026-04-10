# Local Coding Agent

A local coding agent with LLM integration, MCP tools, multi-agent orchestration, and session persistence.

## Features

- **Local & Remote LLM Support**: Flexible model routing between Ollama (local) and cloud APIs
- **MCP-Based Tool Integration**: Standardized access to file system, git, and code analysis
- **Session Persistence**: Conversation history persists across restarts
- **Git Integration**: Full git workflow support (status, diff, commit, log, branches)
- **Cost Tracking**: Monitor API usage and costs
- **Rate Limiting**: Built-in rate limiting to prevent API throttling
- **Streaming Responses**: Real-time feedback during generation

## Documentation

- [User Manual](docs/user-manual.md) - Complete usage guide
- [Examples](docs/examples.md) - Practical examples and workflows
- [Capabilities](docs/capabilities.md) - What the agent can and cannot do
- [Implementation Plan](docs/plans/implementation_plan_v2.md) - Technical roadmap

## Quick Start

### Prerequisites

- Python 3.11+
- Ollama or OpenAI-compatible API (for local models)

### Installation

```powershell
cd J:\Projects\coding-agent
C:\Users\arozz\AppData\Local\Programs\Python\Python313\python.exe -m pip install -e .
```

Or install dependencies directly:

```powershell
C:\Users\arozz\AppData\Local\Programs\Python\Python313\python.exe -m pip install pydantic pydantic-settings langgraph httpx structlog chromadb pytest
```

### Configuration

Edit `config/models.yaml` to configure your LLM:

```yaml
models:
  - name: qwen3.5-35b-a3b
    type: local
    endpoint: http://127.0.0.1:1234  # Your Ollama/OpenAI-compatible endpoint
    context_window: 262144
    is_coding_optimized: true
    recommended_for: [coding, code_review, test_generation]
    rate_limit_rpm: 120
```

## Usage

### Single Task Mode

```bash
python -m local_coding_agent --task "Write a hello world script"
```

### Interactive Mode

```bash
python -m local_coding_agent
```

Interactive commands:
- `exit` / `quit` - Exit the program
- `history` - Show conversation history
- `sessions` - List all sessions
- `resume <id>` - Switch to another session

### Session Management

```bash
# List all sessions
python -m local_coding_agent --list-sessions

# Resume a specific session
python -m local_coding_agent --session session_20260410_123456

# Disable conversation history in context
python -m local_coding_agent --task "..." --no-history
```

## CLI Options

| Option | Description |
|--------|-------------|
| `--task <text>` | Run a single task and exit |
| `--session <id>` | Resume a specific session |
| `--workspace <path>` | Set workspace directory |
| `--config <path>` | Path to model configuration |
| `--list-sessions` | List all saved sessions |
| `--no-history` | Don't include conversation history |
| `--verbose` | Show detailed logging |
| `--stream` | Stream responses in real-time |

## Available Tools

### File System
- `read_file` - Read file contents
- `write_file` - Write content to files
- `list_directory` - List directory contents
- `search_files` - Find files by pattern

### Git
- `git_status` - Show working tree status
- `git_diff` - Show changes
- `git_diff_staged` - Show staged changes
- `git_commit` - Commit changes
- `git_log` - Show commit history
- `git_branch` - List branches
- `git_add` - Stage files
- `git_restore` - Unstage files

### Test Runner
- `pytest_run` - Run pytest tests with configurable options
- `pytest_list` - List available tests
- `pytest_by_marker` - Run tests by marker

### Code Analysis
- `analyze_file` - Extract functions, classes, imports, dependencies
- `analyze_directory` - Batch analyze multiple files
- `find_function` - Find function by name
- `get_function_at_line` - Get function containing specific line

### Code Chunking
- Language-specific code chunking for vector storage
- Supports Python, JavaScript, TypeScript, Java, Go, Rust, and more
- Respects function/class boundaries

## Multi-Agent Workflow

The agent supports a LangGraph-based multi-agent workflow with three nodes:

- **Planner** - Analyzes task and creates step-by-step execution plan
- **Executor** - Executes the plan using available tools
- **Reviewer** - Evaluates result, approves or requests retry (up to max_iterations)

```python
from agent import MultiAgentOrchestrator

orchestrator = MultiAgentOrchestrator(
    workspace_path="./workspace",
    model_router=model_router,
)
result = await orchestrator.run_task("Write a hello world", session_id)
```

## Project Structure

```
local-coding-agent/
├── agent/                    # Core agent
│   ├── orchestrator.py       # Main agent orchestration
│   ├── multi_agent/          # LangGraph multi-agent workflow
│   │   ├── workflow.py       # Planner, Executor, Reviewer nodes
│   │   └── __init__.py
│   ├── memory/              # Session and vector memory
│   │   ├── session_memory.py
│   │   └── codebase_memory.py
│   └── tools/               # Tool implementations
│       ├── file_system_tool.py
│       ├── git_tool.py
│       ├── test_runner_tool.py    # Pytest integration
│       ├── code_analysis_tool.py  # AST parsing
│       └── code_chunker.py        # Language-specific chunking
├── llm/                     # LLM abstraction
│   ├── model_router.py      # Model routing
│   ├── ollama_client.py     # Ollama/OpenAI-compatible client
│   ├── cloud_api_client.py  # Cloud API client
│   ├── cost_tracker.py      # Cost tracking
│   ├── rate_limiter.py      # Rate limiting
│   └── health.py            # Health checks
├── mcp/                     # MCP server
├── observability/            # Metrics and logging
├── config/
│   └── models.yaml          # Model configuration
├── docs/                    # Documentation
│   ├── user-manual.md       # User guide
│   ├── examples.md          # Examples
│   ├── capabilities.md      # Capabilities reference
│   └── plans/               # Implementation plans
└── tests/                   # Unit tests
```

## Development

### Running Tests

```bash
python -m pytest tests/unit/ -v
```

### Running with Verbose Logging

```bash
python -m local_coding_agent --task "..." --verbose
```

## License

MIT
