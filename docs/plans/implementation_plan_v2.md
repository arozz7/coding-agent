---
title: Implementation Plan - Local Coding Agent with Application Development Capabilities
created: 2026-04-06
updated: 2026-04-09
version: 2.0
tags: ["implementation", "plan", "coding-agent", "local-agent", "architecture"]
relatedTopics: ["local-agent", "coding-agent"]
status: draft
---

# Implementation Plan: Local Coding Agent with Application Development Capabilities

## Executive Summary

This document provides a comprehensive implementation plan for building a **local coding agent** capable of developing full-stack applications. The agent integrates insights from both `local-agent` and `coding-agent` research, combining Claude Skills patterns, MCP (Model Context Protocol), multi-agent orchestration, long-term memory, and robust sandboxing for safe execution.

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

### Technology Stack

| Component | Recommendation |
|-----------|---------------|
| **Language** | Python 3.11+ |
| **LLM Runtime (Local)** | Ollama |
| **Primary Model** | `qwen2.5-coder:32b` (~20GB VRAM) |
| **Fallback Model** | `glm-4.7-flash` (~25GB VRAM) or cloud API |
| **Agent Framework** | LangGraph (complex workflows) / CrewAI (simple teams) |
| **MCP SDK** | Python `mcp` package + custom servers |
| **Session Memory** | SQLite |
| **Vector Memory** | ChromaDB |
| **Graph Memory** | NetworkX + Custom MemoryWiki |
| **Sandboxing** | Docker containers / Firecracker microVMs |
| **Observability** | Prometheus metrics + structured logging |

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

### Directory Layout

```
local-coding-agent/
├── agent/                          # Core agent orchestration
│   ├── __init__.py
│   ├── orchestrator.py             # Main agent coordinator (LangGraph/CrewAI)
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
│       ├── file_system_tool.py      # File operations
│       ├── git_tool.py              # Git operations
│       ├── code_analysis_tool.py    # AST parsing, dependencies
│       ├── test_tool.py             # Test execution
│       └── terminal_tool.py         # Sandbox command execution
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
│   └── firecracker_executor.py      # MicroVM execution (advanced)
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
├── skills/                          # User-facing skills (Claude Code style)
│   ├── code-review/
│   │   └── SKILL.md
│   ├── test-generation/
│   │   └── SKILL.md
│   └── deployment/
│       └── SKILL.md
├── logs/                            # Runtime logs
├── data/                            # Persistent data
│   ├── memory.db                    # SQLite session database
│   ├── chroma_db/                   # Vector store
│   └── backups/                     # Backup storage
├── scripts/                         # Utility scripts
│   ├── init_project.py              # Project scaffolding
│   ├── benchmark_models.py          # Model performance testing
│   └── backup.py                    # Backup and recovery scripts
├── pyproject.toml                   # Python dependencies
├── README.md                        # Project documentation
└── .env.example                     # Environment template
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
- [ ] Implement `llm/circuit_breaker.py` for fault tolerance
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

- [ ] Set up MCP server with stdio transport (`mcp/server.py`)
- [ ] Implement filesystem MCP server
- [ ] Add tool discovery endpoint (`tools/list`)
- [ ] Add tool call endpoint (`tools/call`)
- [ ] Set up Prometheus metrics endpoint

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

- [ ] Implement `memory/session_memory.py` with conversation history
- [ ] Add task tracking and completion status
- [ ] Create session lifecycle management
- [ ] Write integration tests

#### Day 4-7: ChromaDB Vector Store with Code-Aware Chunking

**Tasks**:

- [ ] Implement `memory/codebase_memory.py` with file indexing
- [ ] Implement code-aware chunking (respect function/class boundaries)
- [ ] Create RAG retrieval functions
- [ ] Write tests for vector search

### Week 4: Memory Integration & RAG Patterns

#### Day 1-3: Memory Wiki Prototype

**Tasks**:

- [ ] Create basic `memory/memory_wiki.py` with NetworkX graph
- [ ] Implement file dependency tracking
- [ ] Add function call relationship tracking
- [ ] Write unit tests

#### Day 4-7: RAG-Based Code Retrieval in Agent

**Tasks**:

- [ ] Integrate vector search into agent workflow
- [ ] Create context-building from retrieved code
- [ ] Test with multi-file projects
- [ ] Optimize retrieval for coding tasks

---

## 4. Phase 3: Multi-Agent Setup (Weeks 5-6)

### Week 5: Agent Roles & CrewAI Integration

#### Day 1-2: Define Agent Roles

**Tasks**:

- [ ] Create `agents/base_agent.py` abstract class
- [ ] Implement `agents/architect_agent.py` for architecture design
- [ ] Implement `agents/developer_agent.py` for code implementation
- [ ] Implement `agents/reviewer_agent.py` for code review

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

- [ ] Implement subagent spawning mechanism
- [ ] Add context isolation between agents
- [ ] Create result aggregation logic
- [ ] Share session memory across agent team
- [ ] Coordinate codebase memory updates

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

---

## 10. Model Configuration

### Model Selection Strategy

| Task Type | Recommended Model | Reason |
|-----------|------------------|---------|
| **Code Generation** | `qwen2.5-coder:32b` | Optimized for coding tasks |
| **Architecture Design** | `glm-4.7-flash` | Large context window (128K) |
| **Complex Reasoning** | `claude-3.5-sonnet` (cloud) | Best reasoning capability |
| **Quick Iterations** | `qwen2.5-coder:7b` | Faster, good for prototyping |
| **Testing/Validation** | `qwen2.5-coder:32b` | Precise code understanding |

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
| Context overflow | Medium | High | Subagent spawning, context compression |
| Hallucinated code | Medium | Medium | Validation tests, human checkpoints |
| Security breach | Low | Critical | Sandboxing, permission models |
| Model unavailable | Low | Medium | Fallback model, circuit breaker |
| API rate limiting | Medium | Medium | Rate limiter, exponential backoff |
| Cost overruns | Medium | Medium | Cost tracking, usage alerts |

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
- [ ] Full test suite green
- [ ] Backup/recovery tested

---

*Version 2.0 - Added: streaming, cost tracking, rate limiting, retry logic, health checks, Prometheus metrics, code-aware chunking, circuit breaker, backup/recovery, cost guard*

*Status: draft - awaiting review*
