---
title: Implementation Plan - Local Coding Agent with Application Development Capabilities
created: 2026-04-06
updated: 2026-04-10
version: 2.3
tags: ["implementation", "plan", "coding-agent", "local-agent", "architecture"]
relatedTopics: ["local-agent", "coding-agent", "anthropic-managed-agents", "llm-wiki"]
status: draft
---

# Implementation Plan: Local Coding Agent with Application Development Capabilities

## Executive Summary

This document provides a comprehensive implementation plan for building a **local coding agent** capable of developing full-stack applications. The agent integrates insights from:
- Anthropic's Managed Agents architecture (brain/hands decoupling)
- LLM Wiki pattern (persistent knowledge compounding)
- Claude Skills patterns
- MCP (Model Context Protocol)
- Multi-agent orchestration
- Long-term memory with vector DB + MemoryWiki

### Key Capabilities

- **Local & Remote LLM Support**: Flexible model routing between Ollama (local) and cloud APIs
- **MCP-Based Tool Integration**: Standardized access to file system, code analysis, git, testing
- **Claude Skills Framework**: Specialized capabilities for coding workflows
- **Multi-Agent Orchestration**: CrewAI/LangGraph for role-based teams or graph-based workflows
- **Long-Term Memory**: Vector DB + MemoryWiki for codebase-aware knowledge
- **Subagent Spawning**: Context control for large projects
- **Sandboxed Execution**: Secure, isolated environments for code execution
- **Human-in-the-Loop**: Checkpoints for critical decisions
- **Streaming Responses**: Real-time feedback during LLM generation
- **Cost Tracking**: Monitor API usage and costs across cloud providers
- **Resilient Operations**: Retry logic, rate limiting, and health checks
- **Brain/Hand Decoupling**: Formal tool execution interface for replaceable components
- **Persistent Wiki**: Agent-maintained knowledge base that compounds over time

### Technology Stack

| Component | Recommendation |
|-----------|---------------|
| **Language** | Python 3.11+ (cross-platform) |
| **LLM Runtime (Local)** | Ollama (Windows/macOS/Linux) |
| **Primary Model** | `qwen3.5-35b-a3b` (~20GB VRAM) |
| **Fallback Model** | `glm-4.7-flash` (~25GB VRAM) or cloud API |
| **Agent Framework** | LangGraph (complex workflows) / CrewAI (simple teams) |
| **MCP SDK** | Python `mcp` package + custom servers |
| **Session Memory** | SQLite (cross-platform) |
| **Vector Memory** | ChromaDB (cross-platform) |
| **Graph Memory** | NetworkX + Custom MemoryWiki |
| **Sandboxing** | Docker containers / Firecracker microVMs / Windows Sandbox |
| **Observability** | Prometheus metrics + structured logging |
| **Cross-Platform** | `pathlib`, `os.path`, platform-specific detection |

### Architecture Updates (v2.1 - Based on Anthropic Managed Agents & LLM Wiki)

#### Brain/Hand Decoupling
Following Anthropic's Managed Agents architecture, we decouple the "brain" (LLM + harness) from the "hands" (execution environment):

| Component | Before | After |
|-----------|--------|-------|
| **Tool Execution** | Direct in-process calls | `execute(name, input) → output` interface |
| **Session Storage** | Embedded in context | External append-only log with `getEvents()` |
| **Crash Recovery** | Single point of failure | Resume from last event via `wake(sessionId)` |
| **Sandbox** | Same container as brain | Independent tool, provisioned on-demand |

#### Tool Execution Interface
```python
class ToolExecutor:
    def execute(self, tool_name: str, input: dict) -> str:
        """Formal tool interface - execute(name, input) → output"""
        
# Usage
result = executor.execute("shell", {"command": "npm run build"})
result = executor.execute("file_read", {"path": "src/main.py"})
result = executor.execute("screenshot", {"url": "http://localhost:8080"})
```

#### Queryable Session Memory
```python
class SessionMemory:
    def get_events(self, session_id: str, offset: int = 0, limit: int = 100) -> List[dict]:
        """Query events by position - enables programmatic context access"""
        
# Usage
events = session.get_events(session_id, offset=100, limit=50)  # events 100-149
events = session.get_events(session_id, offset=-50)  # last 50 events
```

#### LLM Wiki Pattern
Following Andrej Karpathy's LLM Wiki pattern, the agent maintains a persistent knowledge base:

```
workspace/.agent-wiki/
├── index.md          # Catalog of all entries
├── log.md            # Chronological compilation log
├── tech-patterns/    # Technical patterns discovered
├── bugs/             # Bug fixes and workarounds
├── decisions/        # Architectural decisions
├── api-usage/        # API usage patterns
└── synthesis/        # Cross-domain insights
```

**Wiki Skills:**
- `wiki-compile` - Compile learned patterns to persistent wiki
- `wiki-query` - Query wiki before answering (prevents rediscovery)
- `wiki-lint` - Health-check for contradictions/staleness/orphans

**Benefits:**
- Knowledge compounds over time (no re-derivation on every query)
- Cross-references already exist (no search from scratch)
- Contradictions flagged for human resolution
- Staleness detected automatically

### Cross-Platform Requirements

The agent must function identically on Windows, macOS, and Linux with the following considerations:

| Feature | Windows | macOS | Linux |
|---------|---------|-------|-------|
| **Shell** | PowerShell / CMD | bash/zsh | bash |
| **Paths** | `C:\Users\...` | `/Users/...` | `/home/...` |
| **Default Shell** | PowerShell | bash | bash |
| **Ollama** | `ollama serve` | `ollama serve` | `ollama serve` |
| **Git** | Git for Windows | System git | System git |
| **Docker** | Docker Desktop | Docker Desktop | Docker Engine |
| **Virtual Env** | `python -m venv` | `python3 -m venv` | `python3 -m venv` |

#### Path Handling Best Practices

- Always use `pathlib.Path` for path operations
- Use `os.path.join()` for constructing paths
- Use `/` in strings (works on all platforms in Python)
- Detect platform via `sys.platform` or `platform.system()`
- Store paths as strings in config (relative paths preferred)

#### Shell Detection Logic

```python
import platform
import shutil

def get_shell():
    system = platform.system()
    if system == "Windows":
        # Prefer PowerShell if available
        return shutil.which("pwsh") or "powershell"
    return shutil.which("bash") or "sh"
```

---

## Table of Contents

1. [Project Structure & Architecture](#1-project-structure--architecture)
2. [Phase 1: Foundation (Weeks 1-2)](#phase-1-foundation-weeks-1-2)
3. [Phase 2: Memory Integration (Weeks 3-4)](#phase-2-memory-integration-weeks-3-4)
4. [Phase 3: Multi-Agent Setup (Weeks 5-6)](#phase-3-multi-agent-setup-weeks-5-6)
5. [Phase 4: Advanced Features (Weeks 7-8)](#phase-4-advanced-features-weeks-7-8)
6. [Phase 5: Production Hardening (Weeks 9-10)](#phase-5-production-hardening-weeks-9-10)
7. [Security & Sandboxing](#7-security--sandboxing)
8. [Tool Specifications](#8-tool-specifications)
9. [Skill Definitions](#9-skill-definitions)
10. [Model Configuration](#10-model-configuration)
11. [Resilient LLM Operations](#11-resilient-llm-operations)
12. [Observability & Metrics](#12-observability--metrics)
13. [Backup & Recovery](#13-backup--recovery)
14. [Risk Mitigation](#14-risk-mitigation)

---

## 1. Project Structure & Architecture

### Cross-Platform Project Layout

```
local-coding-agent/
├── agent/                          # Core agent orchestration
│   ├── __init__.py
│   ├── orchestrator.py             # Main agent coordinator (LangGraph/CrewAI)
│   ├── platform.py                 # Platform detection & utilities
│   ├── shell.py                    # Cross-platform shell execution
│   ├── memory/                     # Memory layer
│   │   ├── __init__.py
│   │   ├── session_memory.py       # SQLite session storage
│   │   ├── codebase_memory.py      # ChromaDB vector store
│   │   └── memory_wiki.py          # Graph-based knowledge (NetworkX)
│   ├── agents/                     # Multi-agent definitions
│   │   ├── __init__.py
│   │   ├── base_agent.py           # Abstract agent class
│   │   ├── architect_agent.py       # Architecture design role
│   │   ├── developer_agent.py       # Code implementation role
│   │   ├── reviewer_agent.py        # Code review role
│   │   └── tester_agent.py          # Test generation role
│   ├── skills/                     # Claude Skills integration
│   │   ├── __init__.py
│   │   ├── skill_manager.py         # Skill loading & discovery
│   │   └── skills/                  # Custom skills (see Section 9)
│   └── tools/                      # MCP tool wrappers
│       ├── __init__.py
│       ├── base_tool.py            # Abstract tool base class
│       ├── file_system_tool.py      # File operations (cross-platform)
│       ├── git_tool.py              # Git operations
│       ├── code_analysis_tool.py    # AST parsing, dependencies
│       ├── test_tool.py             # Test execution
│       ├── terminal_tool.py         # Sandbox command execution
│       └── sandbox/                 # Platform-specific sandbox
│           ├── __init__.py
│           ├── base.py              # Base sandbox interface
│           ├── docker_sandbox.py    # Docker-based (Linux/macOS)
│           ├── windows_sandbox.py   # Windows Sandbox
│           └── native_sandbox.py    # Process isolation (all)
├── mcp/                            # MCP servers
│   ├── __init__.py
│   ├── server.py                    # Main MCP server
│   ├── transport/                  # Transport implementations
│   │   ├── stdio.py                # Stdio transport (local)
│   │   └── http.py                 # HTTP transport (remote)
│   └── tools/                      # MCP tool definitions
│       ├── filesystem_server.py
│       ├── git_server.py
│       ├── code_analysis_server.py
│       └── test_runner_server.py
├── llm/                            # LLM abstraction layer
│   ├── __init__.py
│   ├── model_router.py              # Local vs. remote routing
│   ├── ollama_client.py             # Local Ollama client
│   ├── cloud_api_client.py          # Cloud API (Anthropic, OpenAI)
│   ├── streaming.py                 # Streaming response support
│   ├── cost_tracker.py              # API usage and cost tracking
│   ├── rate_limiter.py              # Rate limiting for API calls
│   ├── health.py                    # Health check endpoints
│   ├── retry.py                     # Retry with exponential backoff
│   └── circuit_breaker.py           # Circuit breaker pattern
├── sandbox/                         # Sandboxing infrastructure
│   ├── __init__.py
│   ├── sandbox_manager.py           # Container lifecycle management
│   ├── docker_executor.py           # Docker-based execution
│   ├── firecracker_executor.py      # MicroVM execution (advanced)
│   └── windows_executor.py          # Windows Sandbox execution
├── observability/                   # Observability layer
│   ├── __init__.py
│   ├── metrics.py                   # Prometheus metrics
│   ├── tracing.py                   # Distributed tracing
│   ├── logging.py                   # Structured logging
│   └── routes.py                    # Metrics endpoints
├── config/                          # Configuration files
│   ├── default.yaml                 # Default configuration
│   ├── models.yaml                  # Model configurations
│   ├── permissions.yaml             # Permission rules
│   └── backup.yaml                  # Backup configuration
├── tests/                           # Test suite
│   ├── unit/
│   ├── integration/
│   └── e2e/
│       ├── test_windows.py          # Windows-specific tests
│       ├── test_macos.py            # macOS-specific tests
│       └── test_linux.py            # Linux-specific tests
├── skills/                          # User-facing skills (Claude Code style)
│   ├── code-review/
│   │   └── SKILL.md
│   ├── test-generation/
│   │   └── SKILL.md
│   └── deployment/
│       └── SKILL.md
├── scripts/                         # Utility scripts
│   ├── init_project.py              # Project scaffolding
│   ├── benchmark_models.py          # Model performance testing
│   ├── backup.py                    # Backup and recovery scripts
│   ├── install_ollama.py            # Platform-specific Ollama setup
│   └── platform_check.py           # Verify platform requirements
├── pyproject.toml                   # Python dependencies
├── README.md                        # Project documentation
├── .env.example                     # Environment template
└── .gitignore                      # Git ignore (platform-specific)
```

---

## 2. Phase 1: Foundation (Weeks 1-2)

### Week 1: Project Setup & LLM Integration

#### Day 1-2: Project Initialization

**Tasks**:

- [ ] Initialize Python project with `poetry` or `pipx`
- [ ] Set up virtual environment and install base dependencies
- [ ] Configure pre-commit hooks (black, isort, mypy)
- [ ] Create directory structure from Section 1
- [ ] Set up observability module with Prometheus metrics

**Dependencies**:
```toml
# pyproject.toml
[tool.poetry.dependencies]
python = "^3.11"
langgraph = "^1.1"
crewai = "^1.13"
mcp = "^1.0"
chromadb = "^0.6"
networkx = "^3.3"
pydantic = "^2.8"
pydantic-settings = "^2.3"
sqlalchemy = "^2.0"
python-dotenv = "^1.0"
prometheus-client = "^0.20"
tenacity = "^8.2"
slowapi = "^0.1"
structlog = "^24.2"
httpx = "^0.27"

[tool.poetry.group.dev.dependencies]
pytest = "^8.2"
pytest-asyncio = "^0.23"
black = "^24.4"
isort = "^5.13"
mypy = "^1.10"
```

#### Day 3-4: LLM Abstraction Layer with Resilience

**Tasks**:

- [ ] Implement `llm/model_router.py` with local/remote routing logic
- [ ] Create `llm/ollama_client.py` wrapper for Ollama API
- [ ] Create `llm/cloud_api_client.py` wrapper for Anthropic/OpenAI APIs
- [ ] Implement `llm/streaming.py` for real-time response streaming
- [ ] Implement `llm/cost_tracker.py` for usage and cost tracking
- [ ] Implement `llm/rate_limiter.py` for API throttling prevention
- [ ] Implement `llm/health.py` for model health checks
- [ ] Implement `llm/retry.py` for retry with exponential backoff
- [x] Implement `llm/circuit_breaker.py` for fault tolerance
- [ ] Add model configuration in `config/models.yaml`

#### Day 5-7: Basic File System Tools with Error Handling

**Tasks**:

- [ ] Implement `tools/file_system_tool.py` with path validation
- [ ] Add directory traversal protection
- [ ] Implement comprehensive error handling
- [ ] Create MCP server wrapper for file operations
- [ ] Write unit tests for file operations

### Week 2: MCP Server Setup & Observability

#### Day 1-3: MCP Server Implementation

**Tasks**:

- [x] Set up MCP server with stdio transport (`mcp/server.py`)
- [x] Implement filesystem MCP server
- [x] Add tool discovery endpoint (`tools/list`)
- [x] Add tool call endpoint (`tools/call`)
- [x] Expose MCP via FastAPI at `/mcp/tools` and `/mcp/tools/{name}`
- [x] Register additional tools: shell, tests, code analysis
- [x] Set up resilience API endpoints: `/ready`, `/stats`, `/llm/health`

#### Day 4-5: Basic LangGraph Agent

**Tasks**:

- [ ] Implement basic agent state schema (`orchestrator.py`)
- [ ] Create planner node for task decomposition
- [ ] Add executor node for code generation with streaming
- [ ] Integrate cost tracking into agent
- [ ] Test with simple coding tasks

#### Day 6-7: Integration Testing

**Success Criteria**:

- [ ] Agent can read/write files safely
- [ ] MCP server responds to tool calls
- [ ] Basic LangGraph workflow executes without errors
- [ ] LLM routing works for both local and cloud models
- [ ] Streaming responses provide real-time feedback
- [ ] Cost tracking records usage accurately
- [ ] Rate limiting prevents API throttling

---

## 3. Phase 2: Memory Integration (Weeks 3-4)

### Week 3: Session & Vector Memory

#### Day 1-3: SQLite Session Memory

**Tasks**:

- [x] Implement `memory/session_memory.py` with conversation history
- [x] Add task tracking and completion status
- [x] Create session lifecycle management
- [x] Add `get_events(offset, limit)` for queryable history
- [x] Add `get_event_count()` for total event tracking
- [ ] Write integration tests

#### Day 4-7: ChromaDB Vector Store with Code-Aware Chunking

**Tasks**:

- [x] Implement `memory/codebase_memory.py` with file indexing
- [x] Implement code-aware chunking (respect function/class boundaries)
- [x] Create RAG retrieval functions
- [x] Write tests for vector search
- [x] Add auto-indexing of workspace files
- [x] Integrate RAG context into agent prompts
- [x] Add indexing/search API endpoints

### Week 4: Memory Wiki & Tool Executor

**Tasks**:

- [x] Create `memory/memory_wiki.py` with NetworkX graph
- [x] Implement file dependency tracking
- [x] Add function call relationship tracking
- [x] Create ToolExecutor with formal `execute(name, input)` interface
- [x] Register tools: shell, file_read, file_write, file_list, screenshot, search

#### Day 4-7: RAG-Based Code Retrieval in Agent

**Tasks**:

- [x] Integrate vector search into agent workflow
- [x] Create context-building from retrieved code
- [x] Add wiki context loading to orchestrator (auto-query on tasks)
- [x] Create wiki skills:
  - `skills/wiki-compile/SKILL.md` - compile patterns to persistent wiki
  - `skills/wiki-query/SKILL.md` - query wiki before answering
  - `skills/wiki-lint/SKILL.md` - health-check for issues
- [x] Test with multi-file projects
- [x] Optimize retrieval for coding tasks
- [x] Add skill loading mechanism for wiki and other skills

### Week 4.5: Skill Loading System

**Tasks**:

- [ ] Implement `skills/skill_loader.py` - discover and load skills from `skills/` directory
- [ ] Implement `SkillManager` class with:
  - `discover_skills()` - scan for SKILL.md files
  - `get_skill(name)` - load skill by name
  - `detect_triggers(task)` - keyword-based skill activation
  - `execute_skill(name, context)` - run skill logic
- [ ] Add keyword trigger system from SKILL.md frontmatter
- [ ] Integrate skill loader into orchestrator
- [ ] Add pre-execution hooks (run skills BEFORE agent runs)
- [ ] Add post-execution hooks (run skills AFTER agent completes)

#### Skill Categories

| Trigger Type | Skills |
|--------------|--------|
| **Pre-execution** | tdd-enforcer, security-auditor, architect-adr |
| **Post-execution** | wiki-compile, handover |
| **On-demand** | codebase-mapper, wiki-lint, workspace-janitor |
| **Tool-based** | playwright-cli, browser_tool (screenshots) |

#### Skill Execution Flow

```python
class SkillManager:
    async def process_task(self, task: str, context: dict) -> dict:
        # 1. Detect pre-execution triggers
        pre_skills = self.detect_triggers(task, phase="pre")
        for skill in pre_skills:
            await self.execute_skill(skill, context)
        
        # 2. Run agent
        result = await self.agent.run(task, context)
        
        # 3. Detect post-execution triggers
        post_skills = self.detect_triggers(task, phase="post")
        for skill in post_skills:
            await self.execute_skill(skill, context)
        
        return result
```

---

## 4. Phase 3: Multi-Agent Setup (Weeks 5-6)

### Week 5: Agent Roles & CrewAI Integration

#### Day 1-2: Define Agent Roles

**Tasks**:

- [ ] Create `agents/base_agent.py` abstract class
- [x] Implement `agents/architect_agent.py` for architecture design
- [x] Implement `agents/developer_agent.py` for code implementation
- [x] Implement `agents/reviewer_agent.py` for code review
- [x] Implement `agents/tester_agent.py` for test generation

#### Day 3-4: CrewAI Team Setup

**Tasks**:

- [ ] Configure CrewAI with role-based agents
- [ ] Set up hierarchical process for task delegation
- [ ] Define tasks in YAML configuration
- [ ] Test multi-agent collaboration

#### Day 5-7: LangGraph Alternative Workflow

**Tasks**:

- [ ] Implement graph-based workflow as alternative
- [ ] Add conditional routing for test pass/fail
- [ ] Support iterative refinement loops
- [ ] Compare performance with CrewAI approach

### Week 6: Subagent Spawning & Context Control

**Tasks**:

- [x] Implement subagent spawning mechanism in orchestrator
- [x] Add context isolation between agents (is_subagent flag, isolated context)
- [x] Create result aggregation logic (parent session updates)
- [x] Share session memory across agent team
- [ ] Add subagent lifecycle management (timeout, cleanup)
- [ ] Coordinate codebase memory updates
- [ ] Add subagent API endpoints: /subagent/spawn, /subagent/spawn-batch, /subagent, /subagent/{id}

---

## 5. Phase 4: Advanced Features (Weeks 7-8)

### Week 7: Enhanced MemoryWiki & Human-in-the-Loop

**Tasks**:

- [ ] Complete graph-based knowledge representation
- [ ] Add impact analysis for code changes
- [ ] Implement version-aware snapshots
- [ ] Add checkpoint system in LangGraph workflow
- [ ] Create approval workflow for critical actions

### Week 8: Performance Optimization & Observability

**Tasks**:

- [ ] Implement context compression strategies
- [ ] Add caching for repeated tool calls
- [ ] Optimize vector search performance
- [ ] Profile and benchmark agent performance
- [ ] Build debugging dashboard
- [ ] Document troubleshooting procedures

---

## 6. Phase 5: Production Hardening (Weeks 9-10)

### Week 9: Security & Sandboxing

**Tasks**:

- [ ] Implement Docker-based sandbox
- [ ] Add resource limits (CPU, memory, network)
- [ ] Create isolated execution environment
- [ ] Define permission levels (READ/WRITE/EXECUTE/ADMIN)
- [ ] Penetration testing of sandbox
- [ ] Dependency vulnerability scanning

### Week 10: Documentation & Release Prep

**Tasks**:

- [ ] Write comprehensive README with usage examples
- [ ] Create API documentation
- [ ] Run full test suite (unit, integration, e2e)
- [ ] Performance benchmarking
- [ ] Version tagging and changelog
- [ ] Prepare for release

---

## 7. Security & Sandboxing

### Permission Model

| Permission | Tools Allowed | Approval Required | Use Case |
|------------|---------------|-------------------|----------|
| **READ** | Read, Glob, Grep | No | Code exploration |
| **WRITE** | All READ + Write | No (within scope) | Implementation |
| **EXECUTE** | WRITE + Bash | Yes | Running scripts |
| **ADMIN** | EXECUTE + network | User confirmation | Deployment |

---

## 8. Tool Specifications

### Essential MCP Tools for Coding Agents

| Tool | Purpose | Implementation |
|------|---------|----------------|
| `read_file` | Read file contents | File system server |
| `write_file` | Write/modify files | File system server |
| `list_directory` | Browse project structure | File system server |
| `search_files` | Find files by pattern | File system server + glob |
| `git_status` | Show uncommitted changes | Git server |
| `git_commit` | Create commit | Git server |
| `git_diff` | Show code changes | Git server |
| `analyze_ast` | Parse code structure | Code analysis server |
| `get_dependencies` | List project dependencies | Code analysis server |
| `run_tests` | Execute test suite | Test runner server |
| `lint_code` | Run linters | Custom tool |
| `format_code` | Apply code formatting | Custom tool |

---

## 9. Skill Definitions

### Custom Skills for Coding Agents

#### Skill 1: Code Review

**Location**: `skills/code-review/SKILL.md`

#### Skill 2: Test Generation

**Location**: `skills/test-generation/SKILL.md`

#### Skill 3: Wiki Compile

**Purpose**: Compile learned patterns from agent sessions into a persistent wiki knowledge base.

**Location**: `skills/wiki-compile/SKILL.md`

**Usage**: Auto-invoked after completing significant tasks or when asked to "save" knowledge.

**Output**: Markdown files in `.agent-wiki/` directories.

#### Skill 4: Wiki Query

**Purpose**: Query the persistent wiki knowledge base before answering questions.

**Location**: `skills/wiki-query/SKILL.md`

**Usage**: Runs automatically on every query to prevent knowledge rediscovery.

**Search Strategy**: Index-first → Tag-based → Keyword → Link traversal.

#### Skill 5: Wiki Lint

**Purpose**: Health-check the agent wiki for contradictions, staleness, orphan pages, and missing cross-references.

**Location**: `skills/wiki-lint/SKILL.md`

**Checks**:
- Cross-reference symmetry (if A→B, B→A should exist)
- Staleness detection (>30 days without updates)
- Orphan detection (pages with no inbound links)
- Contradiction detection (conflicting claims)
- Missing cross-references (suggested links)

---

## 10. Model Configuration

### Model Selection Strategy

| Task Type | Recommended Model | Reason |
|-----------|------------------|---------|
| **Code Generation** | `qwen3.5-35b-a3b` | Optimized for coding tasks |
| **Architecture Design** | `glm-4.7-flash` | Large context window (128K) |
| **Complex Reasoning** | `claude-3.5-sonnet` (cloud) | Best reasoning capability |
| **Quick Iterations** | `qwen2.5-coder:7b` | Faster, good for prototyping |
| **Testing/Validation** | `qwen3.5-35b-a3b` | Precise code understanding |
| **Current Setup** | `qwen3.5-35b-a3b` via Ollama at port 1234 | Local 10-min timeout |

---

## 11. Resilient LLM Operations

### Key Components

1. **Retry with Exponential Backoff** (`llm/retry.py`)
   - Configurable max attempts, delays, and strategies
   - Retriable exception types

2. **Rate Limiting** (`llm/rate_limiter.py`)
   - Token bucket algorithm
   - Per-model RPM configuration
   - Burst allowance

3. **Circuit Breaker** (`llm/circuit_breaker.py`)
   - States: CLOSED, OPEN, HALF_OPEN
   - Configurable failure thresholds
   - Automatic recovery

4. **Health Checker** (`llm/health.py`)
   - Success rate tracking
   - Latency monitoring
   - Consecutive failure detection

---

## 12. Observability & Metrics

### Prometheus Metrics

- `agent_requests_total` - Total agent requests
- `llm_requests_total` - LLM API requests by model
- `tool_calls_total` - Tool invocations
- `agent_request_duration_seconds` - Request latency
- `llm_latency_seconds` - LLM response latency
- `tokens_used_total` - Token usage by model
- `agent_cost_total_dollars` - Total API cost

### Endpoints

- `/metrics` - Prometheus scrape endpoint
- `/health` - Basic health check
- `/ready` - Readiness check with model status
- `/stats` - Agent statistics

---

## 13. Backup & Recovery

### Backup Strategy

- Session database: SQLite backup API (hourly)
- Vector store: Directory copy (daily)
- Config: Git version control + encrypted backups
- Retention: 7 days default

### Recovery Procedures

1. Database corruption → Restore from latest backup
2. Memory loss → Rebuild from git history + file system
3. Configuration error → Roll back to previous version

---

## 14. Risk Mitigation

### Technical Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|----------|
| Context overflow | Medium | High | Subagent spawning, context compression, getEvents() pagination |
| Hallucinated code | Medium | Medium | Validation tests, human checkpoints |
| Security breach | Low | Critical | Sandboxing, permission models |
| Model unavailable | Low | Medium | Fallback model, circuit breaker |
| API rate limiting | Medium | Medium | Rate limiter, exponential backoff |
| Cost overruns | Medium | Medium | Cost tracking, usage alerts |
| Session/Orchestrator crash | Medium | High | Wake from last event via getEvents(), on-demand sandbox provisioning |
| Wiki errors compound | Medium | Medium | wiki-lint checks, human review for contradictions |
| Credentials exposure | Low | Critical | Credentials vault (outside sandbox), MCP OAuth proxy |

### Contingency Plans

1. **Local LLM fails** → Fall back to cloud API
2. **Cloud API rate limited** → Queue requests, apply backoff
3. **Sandbox escapes** → Immediate container kill, audit
4. **Agent loops** → Iteration limits, human intervention
5. **Cost exceeds budget** → Alert user, pause cloud API

---

## Success Criteria

### Phase 1 Completion (Week 2)
- [ ] Agent can create simple files via CLI
- [ ] MCP server responds to tool calls
- [ ] LLM routing works for local and cloud models
- [ ] Streaming responses provide real-time feedback
- [ ] Cost tracking and rate limiting operational

### Phase 2 Completion (Week 4)
- [ ] Session memory persists across calls
- [ ] Codebase indexed with code-aware chunking
- [ ] RAG retrieval returns relevant code
- [ ] MemoryWiki tracks dependencies

### Phase 3 Completion (Week 6)
- [ ] Multi-agent team collaborates
- [ ] Subagents spawn with isolated context
- [ ] Human checkpoints work

### Phase 4 Completion (Week 8)
- [ ] MemoryWiki supports impact analysis
- [ ] Observability dashboard available
- [ ] Performance benchmarks met

### Phase 5 Completion (Week 10)
- [ ] Sandbox prevents unauthorized access
- [ ] Security audit passed
- [ ] Full test suite green (all platforms)
- [ ] Backup/recovery tested
- [ ] Cross-platform compatibility verified

---

## Cross-Platform Testing Strategy

### Test Matrix

| Platform | Python | Ollama | Git | Docker | Tests |
|----------|--------|--------|-----|--------|-------|
| **Windows 10/11** | 3.11+ | ✓ | ✓ | Docker Desktop | `test_windows.py` |
| **macOS 12+** | 3.11+ | ✓ | ✓ | Docker Desktop | `test_macos.py` |
| **Linux (Ubuntu 20.04+)** | 3.11+ | ✓ | ✓ | Docker Engine | `test_linux.py` |

### Required Tests

1. **Path Handling** - Verify `pathlib` works correctly across platforms
2. **Shell Execution** - Test PowerShell (Windows) vs bash (macOS/Linux)
3. **Git Operations** - Verify git commands work identically
4. **File Permissions** - Test read/write/execute on each platform
5. **Ollama Connectivity** - Verify localhost:11434 works
6. **SQLite** - Test database operations
7. **ChromaDB** - Verify vector store works
8. **CLI Entry Point** - Test `python -m local_coding_agent` works

### Platform-Specific Considerations

- **Windows**: Handle `\` vs `/` paths, PowerShell execution policy, long paths
- **macOS**: Handle `.DS_Store`, Apple Silicon vs Intel, Homebrew paths
- **Linux**: Handle different distros, systemd, SELinux/AppArmor

---

*Version 2.1 - Added: cross-platform support (Windows/macOS/Linux), platform detection, shell abstraction, sandbox options (Docker/Windows Sandbox), e2e test matrix*

*Version 2.2 - Added: Anthropic Managed Agents architecture (brain/hand decoupling), LLM Wiki pattern, ToolExecutor interface, getEvents() for queryable session memory, wiki skills (compile/query/lint)*

*Version 2.3 - Added: `/ready`, `/stats`, `/llm/health` endpoints; all LLM resilience modules implemented*

*Status: draft - awaiting review*
