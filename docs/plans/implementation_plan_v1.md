---
title: Implementation Plan - Local Coding Agent with Application Development Capabilities
created: 2026-04-06
updated: 2026-04-06
version: 1.0
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
11. [Risk Mitigation](#11-risk-mitigation)

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
│   │   ├── architect_agent.py      # Architecture design role
│   │   ├── developer_agent.py      # Code implementation role
│   │   ├── reviewer_agent.py       # Code review role
│   │   └── tester_agent.py         # Test generation role
│   ├── skills/                     # Claude Skills integration
│   │   ├── __init__.py
│   │   ├── skill_manager.py        # Skill loading & discovery
│   │   └── skills/                 # Custom skills (see Section 9)
│   └── tools/                      # MCP tool wrappers
│       ├── __init__.py
│       ├── base_tool.py            # Abstract tool base class
│       ├── file_system_tool.py     # File operations
│       ├── git_tool.py             # Git operations
│       ├── code_analysis_tool.py   # AST parsing, dependencies
│       ├── test_tool.py            # Test execution
│       └── terminal_tool.py        # Sandbox command execution
├── mcp/                            # MCP servers
│   ├── __init__.py
│   ├── server.py                   # Main MCP server
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
│   ├── model_router.py             # Local vs. remote routing
│   ├── ollama_client.py            # Local Ollama client
│   └── cloud_api_client.py         # Cloud API (Anthropic, OpenAI)
├── sandbox/                        # Sandboxing infrastructure
│   ├── __init__.py
│   ├── sandbox_manager.py          # Container lifecycle management
│   ├── docker_executor.py          # Docker-based execution
│   └── firecracker_executor.py     # MicroVM execution (advanced)
├── config/                         # Configuration files
│   ├── default.yaml                # Default configuration
│   ├── models.yaml                 # Model configurations
│   └── permissions.yaml            # Permission rules
├── tests/                          # Test suite
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── skills/                         # User-facing skills (Claude Code style)
│   ├── code-review/
│   │   └── SKILL.md
│   ├── test-generation/
│   │   └── SKILL.md
│   └── deployment/
│       └── SKILL.md
├── logs/                           # Runtime logs
├── data/                           # Persistent data
│   ├── memory.db                   # SQLite session database
│   └── chroma_db/                  # Vector store
├── scripts/                        # Utility scripts
│   ├── init_project.py             # Project scaffolding
│   └── benchmark_models.py         # Model performance testing
├── pyproject.toml                  # Python dependencies
├── README.md                       # Project documentation
└── .env.example                    # Environment template

```

### Core Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        User Interface                            │
│                (CLI / VS Code Extension / Web)                   │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Agent Orchestrator                            │
│              (LangGraph State Machine / CrewAI)                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Planner Node → Executor Nodes → Reviewer Node → END    │   │
│  └─────────────────────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│   Memory     │ │    Tools     │ │   Skills     │
│   Layer      │ │   (MCP)      │ │   System     │
│ ┌──────────┐ │ │ ┌──────────┐ │ │ ┌──────────┐ │
│ │SQLite    │ │ │ │File       │ │ │ │Code     │ │
│ │ChromaDB  │ │ │ │Git       │ │ │ │Review   │ │
│ │MemoryWiki│ │ │ │Tests     │ │ │ │Deploy   │ │
│ └──────────┘ │ │ └──────────┘ │ │ └──────────┘ │
└──────────────┘ └──────────────┘ └──────────────┘
        │              │              │
        ▼              ▼              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    LLM Abstraction Layer                         │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Model Router: Routes to Ollama (local) or Cloud API    │   │
│  └─────────────────────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│  Ollama      │ │   Cloud      │ │   Sandbox    │
│  (Local)     │ │   API        │ │   Executor   │
│ qwen2.5-    │ │ Anthropic/   │ │ Docker/      │
│ coder:32b   │ │ OpenAI       │ │ Firecracker  │
└──────────────┘ └──────────────┘ └──────────────┘
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

**Deliverables**:
- Working Python project with dependency management
- Basic CI/CD pipeline (GitHub Actions)

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
sqlalchemy = "^2.0"
python-dotenv = "^1.0"

[tool.poetry.group.dev.dependencies]
pytest = "^8.2"
pytest-asyncio = "^0.23"
black = "^24.4"
isort = "^5.13"
mypy = "^1.10"
```

#### Day 3-4: LLM Abstraction Layer
**Tasks**:
- [ ] Implement `llm/model_router.py` with local/remote routing logic
- [ ] Create `llm/ollama_client.py` wrapper for Ollama API
- [ ] Create `llm/cloud_api_client.py` wrapper for Anthropic/OpenAI APIs
- [ ] Add model configuration in `config/models.yaml`

**Implementation Example**:
```python
# llm/model_router.py
from typing import Optional, List
from .ollama_client import OllamaClient
from .cloud_api_client import CloudAPIClient
from pydantic import BaseModel

class ModelConfig(BaseModel):
    name: str
    type: str  # "local" or "remote"
    endpoint: Optional[str] = None
    api_key: Optional[str] = None
    context_window: int = 32000
    is_coding_optimized: bool = False

class ModelRouter:
    def __init__(self, config_path: str = "config/models.yaml"):
        self.configs = self._load_configs(config_path)
        self.ollama = OllamaClient()
        self.cloud = CloudAPIClient()
    
    def _load_configs(self, path: str) -> List[ModelConfig]:
        # Load from YAML config
        pass
    
    def get_model(self, purpose: str = "general") -> ModelConfig:
        """Select appropriate model based on task type"""
        if purpose == "coding":
            return next(m for m in self.configs if m.is_coding_optimized)
        return self.configs[0]  # Default
    
    async def generate(self, prompt: str, config: ModelConfig) -> str:
        """Route to appropriate LLM backend"""
        if config.type == "local":
            return await self.ollama.generate(prompt, config.name)
        else:
            return await self.cloud.generate(prompt, config)
```

**`config/models.yaml`**:
```yaml
models:
  - name: qwen2.5-coder:32b
    type: local
    context_window: 32000
    is_coding_optimized: true
    recommended_for: [coding, code_review, test_generation]
  
  - name: glm-4.7-flash
    type: local
    context_window: 128000
    is_coding_optimized: false
    recommended_for: [planning, reasoning, analysis]
  
  - name: claude-3-5-sonnet-20241022
    type: remote
    endpoint: https://api.anthropic.com/v1/messages
    context_window: 200000
    is_coding_optimized: true
    recommended_for: [complex_reasoning, architecture]
```

#### Day 5-7: Basic File System Tools
**Tasks**:
- [ ] Implement `tools/file_system_tool.py` with path validation
- [ ] Add directory traversal protection
- [ ] Create MCP server wrapper for file operations
- [ ] Write unit tests for file operations

**Implementation Example**:
```python
# tools/file_system_tool.py
import os
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field

class FileSystemTool:
    def __init__(self, allowed_base_path: str):
        self.allowed_base = Path(allowed_base_path).resolve()
    
    def _validate_path(self, path: str) -> Path:
        """Prevent directory traversal attacks"""
        resolved = (self.allowed_base / path).resolve()
        if not str(resolved).startswith(str(self.allowed_base)):
            raise ValueError(f"Path outside allowed base: {path}")
        return resolved
    
    def read_file(self, file_path: str) -> str:
        validated = self._validate_path(file_path)
        with open(validated, 'r', encoding='utf-8') as f:
            return f.read()
    
    def write_file(self, file_path: str, content: str) -> None:
        validated = self._validate_path(file_path)
        validated.parent.mkdir(parents=True, exist_ok=True)
        with open(validated, 'w', encoding='utf-8') as f:
            f.write(content)
    
    def list_directory(self, dir_path: str) -> List[dict]:
        validated = self._validate_path(dir_path)
        entries = []
        for item in sorted(validated.iterdir()):
            entries.append({
                "name": item.name,
                "type": "directory" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else None,
                "modified": item.stat().st_mtime
            })
        return entries
    
    def search_files(self, pattern: str, dir_path: str = ".") -> List[str]:
        validated = self._validate_path(dir_path)
        return [str(p.relative_to(validated)) for p in validated.rglob(pattern)]
```

### Week 2: MCP Server Setup & Basic Agent

#### Day 1-3: MCP Server Implementation
**Tasks**:
- [ ] Set up MCP server with stdio transport (`mcp/server.py`)
- [ ] Implement filesystem MCP server
- [ ] Add tool discovery endpoint (`tools/list`)
- [ ] Add tool call endpoint (`tools/call`)

**MCP Server Example**:
```python
# mcp/server.py
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import BaseModel

server = Server("local-coding-agent")

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="read_file",
            description="Read contents of a file",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"}
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="write_file",
            description="Write content to a file",
            inputSchema={...}
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "read_file":
        return file_system_tool.read_file(arguments["path"])
    elif name == "write_file":
        file_system_tool.write_file(
            arguments["path"], 
            arguments["content"]
        )
        return {"success": True}

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options={})

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

#### Day 4-5: Basic LangGraph Agent
**Tasks**:
- [ ] Implement basic agent state schema (`orchestrator.py`)
- [ ] Create planner node for task decomposition
- [ ] Add executor node for code generation
- [ ] Test with simple coding tasks

**LangGraph Example**:
```python
# agent/orchestrator.py
from typing import TypedDict, Annotated, List
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
import operator

class AgentState(TypedDict):
    task_description: str
    current_file: str
    code_content: str
    test_results: str
    iterations: int
    messages: List[dict]

def planner_node(state: AgentState) -> AgentState:
    """Decompose task into coding steps"""
    prompt = f"""Analyze the following task and break it into implementation steps:
    
{state['task_description']}
    
Output a list of files to create/modify with their purpose."""
    
    # Call LLM
    response = model_router.generate(prompt)
    
    return {"messages": [{"role": "assistant", "content": response}]}

def executor_node(state: AgentState) -> AgentState:
    """Generate code based on planning"""
    prompt = f"""Write code for the following requirement:
    
{state['task_description']}

Context from previous iterations:
{state.get('code_content', 'None')}"""
    
    response = model_router.generate(prompt)
    
    return {"code_content": response}

# Build workflow
workflow = StateGraph(AgentState)
workflow.add_node("planner", planner_node)
workflow.add_node("executor", executor_node)

workflow.set_entry_point("planner")
workflow.add_edge("planner", "executor")
workflow.add_edge("executor", END)

app = workflow.compile(checkpointer=MemorySaver())
```

#### Day 6-7: Integration Testing
**Tasks**:
- [ ] Test end-to-end agent flow
- [ ] Verify MCP tool calls work correctly
- [ ] Add logging and error handling
- [ ] Document API usage

**Success Criteria**:
- [ ] Agent can read/write files safely
- [ ] MCP server responds to tool calls
- [ ] Basic LangGraph workflow executes without errors
- [ ] LLM routing works for both local and cloud models

---

## 3. Phase 2: Memory Integration (Weeks 3-4)

### Week 3: Session & Vector Memory

#### Day 1-3: SQLite Session Memory
**Tasks**:
- [ ] Implement `memory/session_memory.py` with conversation history
- [ ] Add task tracking and completion status
- [ ] Create session lifecycle management
- [ ] Write integration tests

**Implementation Example**:
```python
# memory/session_memory.py
import sqlite3
from datetime import datetime
from typing import List, Optional

class SessionMemory:
    def __init__(self, db_path: str = "data/memory.db"):
        self.conn = sqlite3.connect(db_path)
        self._initialize_schema()
    
    def _initialize_schema(self):
        cursor = self.conn.cursor()
        
        # Sessions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                project_path TEXT,
                status TEXT DEFAULT 'active',
                current_task TEXT
            )
        ''')
        
        # Messages table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tokens_used INTEGER DEFAULT 0,
                model_name TEXT,
                tool_calls TEXT  -- JSON array
            )
        ''')
        
        # Tasks table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT REFERENCES sessions(id),
                description TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                result TEXT  -- JSON result data
            )
        ''')
        
        self.conn.commit()
    
    def create_session(self, session_id: str, project_path: str = None) -> str:
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO sessions (id, project_path, status)
            VALUES (?, ?, 'active')
        ''', (session_id, project_path))
        self.conn.commit()
        return session_id
    
    def save_message(self, session_id: str, role: str, content: str,
                     tokens_used: int = 0, model_name: str = None,
                     tool_calls: List[dict] = None) -> None:
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO messages (session_id, role, content, tokens_used, 
                                  model_name, tool_calls)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (session_id, role, content, tokens_used, model_name,
              json.dumps(tool_calls) if tool_calls else None))
        self.conn.commit()
    
    def get_conversation_history(self, session_id: str, 
                                 max_messages: int = 50) -> List[dict]:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT role, content, tokens_used, model_name
            FROM messages
            WHERE session_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (session_id, max_messages))
        
        # Reverse to get chronological order
        messages = cursor.fetchall()[::-1]
        return [{"role": r, "content": c, "tokens": t, "model": m} 
                for r, c, t, m in messages]
    
    def update_task_status(self, session_id: str, task_desc: str, 
                           status: str, result: dict = None) -> int:
        cursor = self.conn.cursor()
        
        # Check if task exists
        cursor.execute('''
            SELECT id FROM tasks WHERE session_id = ? AND description = ?
        ''', (session_id, task_desc))
        existing = cursor.fetchone()
        
        if existing:
            task_id = existing[0]
            cursor.execute('''
                UPDATE tasks SET status = ?, completed_at = CURRENT_TIMESTAMP,
                                 result = ? WHERE id = ?
            ''', (status, json.dumps(result) if result else None, task_id))
        else:
            cursor.execute('''
                INSERT INTO tasks (session_id, description, status, result)
                VALUES (?, ?, ?, ?)
            ''', (session_id, task_desc, status, 
                  json.dumps(result) if result else None))
        
        self.conn.commit()
        return cursor.rowcount
    
    def get_session_summary(self, session_id: str) -> dict:
        """Get high-level summary of session"""
        cursor = self.conn.cursor()
        
        # Count messages
        cursor.execute('SELECT COUNT(*) FROM messages WHERE session_id = ?', 
                      (session_id,))
        message_count = cursor.fetchone()[0]
        
        # Count tasks by status
        cursor.execute('''
            SELECT status, COUNT(*) FROM tasks 
            WHERE session_id = ? GROUP BY status
        ''', (session_id,))
        task_counts = dict(cursor.fetchall())
        
        return {
            "session_id": session_id,
            "message_count": message_count,
            "tasks": task_counts,
            "created_at": self._get_session_created(session_id)
        }
```

#### Day 4-7: ChromaDB Vector Store for Codebase
**Tasks**:
- [ ] Implement `memory/codebase_memory.py` with file indexing
- [ ] Add code chunking strategy (preserve line boundaries)
- [ ] Create RAG retrieval functions
- [ ] Write tests for vector search

**Implementation Example**:
```python
# memory/codebase_memory.py
import chromadb
from chromadb.config import Settings
from pathlib import Path
from typing import List, Optional
import hashlib

class CodebaseMemory:
    def __init__(self, persist_path: str = "data/chroma_db"):
        self.client = chromadb.PersistentClient(
            path=persist_path,
            settings=Settings(anonymizer=None, is_persistent=True)
        )
        
        # Create collections for different knowledge types
        self.files_collection = self.client.get_or_create_collection(
            name="code_files",
            metadata={"hnsw:space": "cosine"}
        )
        
        self.functions_collection = self.client.get_or_create_collection(
            name="functions",
            metadata={"hnsw:space": "cosine"}
        )
    
    def _chunk_code(self, content: str, chunk_size: int = 2000) -> List[str]:
        """Chunk code while preserving line boundaries"""
        lines = content.split('\n')
        chunks = []
        current_chunk = []
        current_size = 0
        
        for line in lines:
            line_size = len(line)
            if current_size + line_size > chunk_size and current_chunk:
                chunks.append('\n'.join(current_chunk))
                current_chunk = []
                current_size = 0
            current_chunk.append(line)
            current_size += line_size
        
        if current_chunk:
            chunks.append('\n'.join(current_chunk))
        
        return chunks
    
    def _compute_hash(self, content: str) -> str:
        """Compute hash for change detection"""
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    
    def index_file(self, file_path: str, content: str, project_id: str) -> None:
        """Index entire file for RAG retrieval"""
        chunks = self._chunk_code(content)
        
        ids = [f"{file_path}:chunk:{i}" for i in range(len(chunks))]
        metadatas = [{
            "file_path": file_path,
            "project_id": project_id,
            "chunk_type": "file",
            "line_start": i * 50,
            "line_end": min((i + 1) * 50, len(content.split('\n')))
        } for i in range(len(chunks))]
        
        self.files_collection.add(
            documents=chunks,
            metadatas=metadatas,
            ids=ids
        )
    
    def index_function(self, file_path: str, function_name: str, 
                       signature: str, docstring: str, project_id: str) -> None:
        """Index individual function for precise retrieval"""
        self.functions_collection.add(
            documents=[f"{signature} {docstring}"],
            metadatas=[{
                "file_path": file_path,
                "function_name": function_name,
                "project_id": project_id,
                "chunk_type": "function"
            }],
            ids=[f"{file_path}:{function_name}"]
        )
    
    def search_files(self, query: str, n_results: int = 5) -> List[dict]:
        """Search for relevant code files via RAG"""
        results = self.files_collection.query(
            query_texts=[query],
            n_results=n_results,
            include=["metadatas", "distances"]
        )
        
        return self._format_results(results['documents'][0], 
                                   results['metadatas'][0],
                                   results['distances'][0])
    
    def search_functions(self, query: str, n_results: int = 5) -> List[dict]:
        """Search for relevant functions"""
        results = self.functions_collection.query(
            query_texts=[query],
            n_results=n_results,
            include=["metadatas", "distances"]
        )
        
        return self._format_results(results['documents'][0],
                                   results['metadatas'][0],
                                   results['distances'][0])
    
    def _format_results(self, documents: List[str], 
                       metadatas: List[dict],
                       distances: List[float]) -> List[dict]:
        """Format search results for agent consumption"""
        formatted = []
        for i in range(len(documents)):
            metadata = metadatas[i]
            distance = distances[i]
            
            formatted.append({
                "type": metadata["chunk_type"],
                "path": metadata["file_path"],
                "content": documents[i],
                "relevance_score": round(1 - distance, 3),
                "metadata": metadata
            })
        
        return formatted
    
    def update_file_if_changed(self, file_path: str, content: str,
                               project_id: str) -> bool:
        """Update file in memory only if content changed"""
        # Check if file exists and compare hash (simplified)
        # In production, track hashes separately
        return True  # Always update for now
```

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

#### Day 1-3: Subagent Implementation
**Tasks**:
- [ ] Implement subagent spawning mechanism
- [ ] Add context isolation between agents
- [ ] Create result aggregation logic
- [ ] Test with complex multi-file tasks

#### Day 4-7: Memory Integration Across Agents
**Tasks**:
- [ ] Share session memory across agent team
- [ ] Coordinate codebase memory updates
- [ ] Implement conflict resolution for shared resources
- [ ] Write integration tests

---

## 5. Phase 4: Advanced Features (Weeks 7-8)

### Week 7: Enhanced MemoryWiki & Human-in-the-Loop

#### Day 1-3: Full MemoryWiki Implementation
**Tasks**:
- [ ] Complete graph-based knowledge representation
- [ ] Add impact analysis for code changes
- [ ] Implement version-aware snapshots
- [ ] Optimize graph queries for large codebases

#### Day 4-7: Human-in-the-Loop Checkpoints
**Tasks**:
- [ ] Add checkpoint system in LangGraph workflow
- [ ] Create approval workflow for critical actions
- [ ] Build UI for human review (CLI or web)
- [ ] Test with destructive operations (deploy, delete)

### Week 8: Performance Optimization & Observability

#### Day 1-3: Performance Optimizations
**Tasks**:
- [ ] Implement context compression strategies
- [ ] Add caching for repeated tool calls
- [ ] Optimize vector search performance
- [ ] Profile and benchmark agent performance

#### Day 4-7: Debugging & Observability
**Tasks**:
- [ ] Add structured logging throughout system
- [ ] Create tracing for agent decisions
- [ ] Build debugging dashboard (CLI or web)
- [ ] Document troubleshooting procedures

---

## 6. Phase 5: Production Hardening (Weeks 9-10)

### Week 9: Security & Sandboxing

#### Day 1-3: Sandbox Implementation
**Tasks**:
- [ ] Implement Docker-based sandbox (`sandbox/docker_executor.py`)
- [ ] Add resource limits (CPU, memory, network)
- [ ] Create isolated execution environment
- [ ] Test security boundaries

#### Day 4-5: Permission Models
**Tasks**:
- [ ] Define READ/WRITE/EXECUTE/ADMIN permission levels
- [ ] Implement permission checking in tools
- [ ] Add explicit approval flows for sensitive operations
- [ ] Audit logging for all privileged actions

#### Day 6-7: Security Testing
**Tasks**:
- [ ] Penetration testing of sandbox
- [ ] Input validation coverage
- [ ] Dependency vulnerability scanning
- [ ] Security review and hardening

### Week 10: Documentation & Release Prep

#### Day 1-3: User Documentation
**Tasks**:
- [ ] Write comprehensive README with usage examples
- [ ] Create API documentation (Sphinx or MkDocs)
- [ ] Build tutorial for first-time users
- [ ] Document configuration options

#### Day 4-5: Testing & Validation
**Tasks**:
- [ ] Run full test suite (unit, integration, e2e)
- [ ] Performance benchmarking against baseline
- [ ] User acceptance testing scenarios
- [ ] Bug fixes and polish

#### Day 6-7: Release Preparation
**Tasks**:
- [ ] Version tagging and changelog
- [ ] Docker image build and registry push
- [ ] CI/CD pipeline finalization
- [ ] Prepare for public release

---

## 7. Security & Sandboxing

### Sandbox Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Agent Process                             │
│              (Untrusted Code Execution)                      │
└───────────────────────┬─────────────────────────────────────┘
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
┌───────────────┐ ┌───────────────┐ ┌───────────────┐
│   Docker      │ │  Firecracker  │ │    gVisor     │
│   Container   │ │    MicroVM    │ │    Sandbox    │
│               │ │               │ │               │
│ - Isolated FS │ │ - Hardware    │ │ - System call │
│ - Limited net │ │   isolation   │ │   filtering   │
│ - Resource    │ │ - Minimal     │ │ - Seccomp     │
│   limits      │ │   kernel      │ │   profiles    │
└───────────────┘ └───────────────┘ └───────────────┘
```

### Recommended Sandbox: Docker with Strict Limits

**Implementation**:
```python
# sandbox/docker_executor.py
import docker
from docker.types import DeviceRequest
from typing import Optional, Dict, Any

class DockerSandbox:
    def __init__(self):
        self.client = docker.from_env()
    
    async def execute_command(self, command: str, timeout: int = 30,
                             working_dir: str = None,
                             environment: Dict[str, str] = None) -> Dict[str, Any]:
        """Execute command in isolated Docker container"""
        
        # Create container with strict resource limits
        container = self.client.containers.run(
            "python:3.11-slim",
            command=f"bash -c '{command}'",
            detach=False,
            network_mode="none",  # No network access
            memory="512m",  # 512MB limit
            cpu_quota=int(50 * 1000),  # 50% CPU
            mem_limit="512m",
            working_dir=working_dir or "/workspace",
            environment=environment or {},
            auto_remove=True,
            timeout=timeout
        )
        
        # Get output
        exit_code = container.attrs["State"]["ExitCode"]
        logs = container.logs().decode('utf-8')
        
        return {
            "exit_code": exit_code,
            "stdout": logs.split("\n")[0] if logs else "",
            "stderr": logs.split("\n")[-1] if logs else "",
            "success": exit_code == 0
        }
    
    def validate_path(self, path: str) -> bool:
        """Ensure path is within allowed workspace"""
        # Implement path validation logic
        return True

# Usage in tools/terminal_tool.py
class TerminalTool:
    def __init__(self):
        self.sandbox = DockerSandbox()
    
    async def execute(self, command: str) -> dict:
        """Execute command with sandboxing"""
        # Validate command (prevent dangerous commands)
        if any(x in command for x in ["rm -rf /", "dd if=", "> /"]):
            raise SecurityError("Dangerous command blocked")
        
        return await self.sandbox.execute_command(command)
```

### Permission Model

| Permission | Tools Allowed | Approval Required | Use Case |
|------------|---------------|-------------------|----------|
| **READ** | Read, Glob, Grep | No | Code exploration |
| **WRITE** | All READ + Write | No (within scope) | Implementation |
| **EXECUTE** | WRITE + Bash | Yes | Running scripts |
| **ADMIN** | EXECUTE + network | User confirmation | Deployment, API calls |

### Security Best Practices

1. **Input Validation**: Sanitize all file paths and command arguments
2. **Command Whitelisting**: Only allow approved commands in sandbox
3. **Network Isolation**: Disable network in default execution mode
4. **Resource Limits**: Cap CPU, memory, and disk usage per execution
5. **Audit Logging**: Log all privileged operations with user attribution
6. **Regular Security Reviews**: Update dependencies and review permissions

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

### Tool Implementation Example

```python
# mcp/tools/git_server.py
from mcp.server import Server
import subprocess
from pathlib import Path

class GitServer:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
    
    async def git_status(self) -> dict:
        """Get git status"""
        result = await self._run_git(["status", "--porcelain"])
        return {"status": result}
    
    async def git_diff(self, path: str = None) -> str:
        """Get diff for specific file or all changes"""
        args = ["diff"]
        if path:
            args.extend([str(path)])
        
        result = await self._run_git(args)
        return result
    
    async def git_commit(self, message: str, files: list = None) -> dict:
        """Create commit with specified files"""
        if files:
            await self._run_git(["add"] + files)
        
        await self._run_git(["commit", "-m", message])
        return {"committed": True}
    
    async def _run_git(self, args: list) -> str:
        """Execute git command in repo"""
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(self.repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            raise RuntimeError(f"Git error: {stderr.decode()}")
        
        return stdout.decode()

# Register with MCP server
git_server = GitServer("/path/to/repo")

@server.call_tool()
async def git_operations(name: str, arguments: dict):
    if name == "git_status":
        return await git_server.git_status()
    elif name == "git_diff":
        return await git_server.git_diff(arguments.get("path"))
    elif name == "git_commit":
        return await git_server.git_commit(
            arguments["message"],
            arguments.get("files")
        )
```

---

## 9. Skill Definitions

### Custom Skills for Coding Agents

#### Skill 1: Code Review

**Location**: `skills/code-review/SKILL.md`

```yaml
---
name: code-review
description: Review code for bugs, security issues, and best practices. Use when reviewing pull requests or before committing changes.
context: fork
agent: Explore
allowed-tools: Read Grep Glob Bash
---

# Code Review Skill

## Quick Start

Review the current changes by running the analysis script:
```bash
python scripts/review_changes.py --strict
```

## Review Process

### Step 1: Analyze structure
- Identify changed files and their dependencies
- Check for breaking API changes

### Step 2: Security scan
```bash
python scripts/security_scan.py $CHANGED_FILES
```

### Step 3: Code quality
- Run linter: `python scripts/lint.py --strict`
- Check formatting: `python scripts/format_check.py`

### Step 4: Generate report
Output markdown summary to `reviews/review-$(date +%Y%m%d).md`

## Guidelines

- Focus on critical bugs and security issues first
- Suggest improvements for readability and maintainability
- Reference project conventions from `CONTRIBUTING.md`
```

#### Skill 2: Test Generation

**Location**: `skills/test-generation/SKILL.md`

```yaml
---
name: test-generation
description: Generate unit tests for code changes. Use after implementing new features or refactoring.
context: fork
agent: Plan
allowed-tools: Read Write Bash
---

# Test Generation Skill

## Workflow

### Step 1: Analyze source
Read the source file and identify functions to test.

### Step 2: Generate tests
Follow our testing patterns from `tests/PATTERNS.md`:
```python
def test_[function_name]():
    """Test case for [function_name]"""
    # Arrange
    input_data = ...
    
    # Act
    result = function(input_data)
    
    # Assert
    assert result == expected_output
```

### Step 3: Run tests
```bash
pytest tests/ -v --cov=src
```

### Step 4: Report coverage
Generate report with `scripts/generate_coverage.py`

## Quality Gates

- Minimum 80% coverage for new code
- All critical paths must have test cases
- Tests should run in <5 seconds
```

#### Skill 3: Project Scaffolding

**Location**: `skills/project-scaffold/SKILL.md`

```yaml
---
name: project-scaffold
description: Create new project structure based on template. Use when starting a new project or microservice.
context: fork
agent: Plan
allowed-tools: Write Bash
disable-model-invocation: true
---

# Project Scaffolding Skill

## Usage

Specify the project type and name:
```bash
python scripts/scaffold.py --type webapp --name myproject
```

## Templates Available

- `webapp`: Full-stack web application (React + Node.js)
- `api`: REST API service (FastAPI/Express)
- `library`: Python/TypeScript library with tests
- `microservice`: Distributed service template

## Generated Structure

The scaffold creates:
- Project structure with proper directory layout
- Configuration files (.env.example, .gitignore)
- README.md with setup instructions
- Initial commit with all files

## Security

- All generated code follows security best practices
- Dependencies are pinned to secure versions
- No hardcoded secrets or credentials
```

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

### Configuration File

```yaml
# config/models.yaml
models:
  # Primary coding model (local)
  qwen2.5-coder-32b:
    type: local
    runtime: ollama
    endpoint: http://localhost:11434
    context_window: 32000
    recommended_for: [code_generation, code_review, test_generation]
    params:
      temperature: 0.3
      top_p: 0.9
      num_predict: 8192
  
  # General purpose model (local)
  glm-4.7-flash:
    type: local
    runtime: ollama
    endpoint: http://localhost:11434
    context_window: 128000
    recommended_for: [planning, architecture, analysis]
    params:
      temperature: 0.5
      top_p: 0.95
      num_predict: 16384
  
  # Cloud fallback (when local unavailable)
  claude-3.5-sonnet:
    type: remote
    runtime: anthropic
    endpoint: https://api.anthropic.com/v1/messages
    context_window: 200000
    recommended_for: [complex_reasoning, architecture]
    api_key_env: ANTHROPIC_API_KEY
    params:
      temperature: 0.7
      max_tokens: 8192

# Fallback configuration
fallback:
  local_priority: true  # Prefer local models when available
  cloud_fallback: true  # Use cloud if local unavailable
  offline_mode: false   # Block execution if no models available

# Model health checks
health_check:
  interval_seconds: 300
  timeout_seconds: 30
  retry_attempts: 3
```

---

## Implementation Issues & Corrections

### Critical Issues Identified

The following issues should be addressed before Phase 1 implementation:

#### 1. **Missing Imports in Code Examples**

**Issue**: Several code examples are missing required imports.

**Corrections**:
```python
# Line 235-272: llm/model_router.py
from typing import Optional, List
from .ollama_client import OllamaClient
from .cloud_api_client import CloudAPIClient
from pydantic import BaseModel
import json  # MISSING
import asyncio  # MISSING

# Line 500-638: memory/session_memory.py
import sqlite3
from datetime import datetime
from typing import List, Optional
import json  # MISSING - Required for tool_calls serialization

# Line 649-781: memory/codebase_memory.py
import chromadb
from chromadb.config import Settings
from pathlib import Path
from typing import List, Optional
import hashlib
import json  # MISSING - Required for metadata serialization

# Line 1057-1118: mcp/tools/git_server.py
from mcp.server import Server
import subprocess
import asyncio  # MISSING - Required for subprocess operations
from pathlib import Path
```

#### 2. **Docker Sandbox Security Vulnerability**

**Issue**: Line 972-984 uses `f"bash -c '{command}'"` which is vulnerable to command injection.

**Correction**:
```python
# Lines 956-1014: sandbox/docker_executor.py
async def execute_command(self, command: str, timeout: int = 30,
                         working_dir: str = None,
                         environment: Dict[str, str] = None) -> Dict[str, Any]:
    """Execute command in isolated Docker container"""
    
    # Create container with strict resource limits
    # Use list form to prevent shell injection
    container = self.client.containers.run(
        "python:3.11-slim",
        command=["python", "-c", command],  # FIXED: List form prevents injection
        detach=False,
        network_mode="none",  # No network access
        memory="512m",  # 512MB limit
        cpu_quota=int(50 * 1000),  # 50% CPU
        mem_limit="512m",
        working_dir=working_dir or "/workspace",
        environment=environment or {},
        auto_remove=True,
        stdout=True,  # FIXED: Capture output properly
        stderr=True
    )
```

#### 3. **MCP Server Import Errors**

**Issue**: Lines 361-408 show incorrect MCP server imports.

**Correction**:
```python
# mcp/server.py
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool  # MISSING import
from pydantic import BaseModel

server = Server("local-coding-agent")

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="read_file",
            description="Read contents of a file",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"}
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="write_file",
            description="Write content to a file",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"]
            }
        )
    ]
```

#### 4. **Model Name Inconsistency**

**Issue**: Text uses `qwen2.5-coder:32b` but config uses `qwen2.5-coder-32b` (dash vs colon).

**Correction** (Line 1288):
```yaml
models:
  # Primary coding model (local)
  qwen2.5-coder:32b:  # FIXED: Use colon, not dash
    type: local
    runtime: ollama
    endpoint: http://localhost:11434
    context_window: 32000
    is_coding_optimized: true
    recommended_for: [code_generation, code_review, test_generation]
    params:
      temperature: 0.3
      top_p: 0.9
      num_predict: 8192
```

#### 5. **ChromaDB API Changes**

**Issue**: Lines 657-661 use deprecated `chromadb.config.Settings` import.

**Correction**:
```python
# memory/codebase_memory.py
import chromadb
from pathlib import Path
from typing import List, Optional
import hashlib
import json

class CodebaseMemory:
    def __init__(self, persist_path: str = "data/chroma_db"):
        # FIXED: Use Client with persist_directory for newer ChromaDB versions
        self.client = chromadb.PersistentClient(
            path=persist_path
        )
        
        # Create collections for different knowledge types
        self.files_collection = self.client.get_or_create_collection(
            name="code_files"
        )
        
        self.functions_collection = self.client.get_or_create_collection(
            name="functions"
        )
```

#### 6. **LangGraph State Update Issue**

**Issue**: Lines 433-457 show incorrect state updates in planner_node and executor_node.

**Correction**:
```python
# agent/orchestrator.py
from typing import TypedDict, Annotated, List
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

class AgentState(TypedDict):
    task_description: str
    current_file: str
    code_content: str
    test_results: str
    iterations: int
    messages: Annotated[List[dict], operator.add]  # FIXED: Use operator.add for append

def planner_node(state: AgentState) -> AgentState:
    """Decompose task into coding steps"""
    prompt = f"""Analyze the following task and break it into implementation steps:
    
{state['task_description']}
    
Output a list of files to create/modify with their purpose."""
    
    # Call LLM
    response = model_router.generate(prompt)
    
    # FIXED: Properly update messages list
    return {"messages": [{"role": "assistant", "content": response}]}

def executor_node(state: AgentState) -> AgentState:
    """Generate code based on planning"""
    prompt = f"""Write code for the following requirement:
    
{state['task_description']}

Context from previous iterations:
{state.get('code_content', 'None')}"""
    
    response = model_router.generate(prompt)
    
    # FIXED: Return updates for multiple fields
    return {
        "code_content": response,
        "iterations": state["iterations"] + 1
    }
```

#### 7. **Skills Frontmatter Clarification**

**Issue**: Lines 1130-1266 use Claude Code-specific frontmatter that won't work in general Python.

**Clarification**: The `context: fork` and `agent: Explore` fields are Claude Code specific. For general Python implementation, use:

```yaml
---
name: code-review
description: Review code for bugs, security issues, and best practices. Use when reviewing pull requests or before committing changes.
---

# Code Review Skill

## Workflow

This skill should be invoked when:
- User requests code review
- Pull request is submitted
- Before committing significant changes

## Process

1. Read changed files
2. Run security scans
3. Check code quality
4. Generate report

## Tools Available

- Read, Write, Glob, Grep (via MCP)
- Bash (for running analysis scripts)

## Expected Output

Markdown report with:
- Critical issues (security, bugs)
- Medium issues (code quality)
- Suggestions for improvement
```

#### 8. **Missing Components to Add**

**Add to Phase 1 or 2**:
1. **Streaming support** for LLM responses (real-time feedback)
2. **Cost tracking** for cloud API usage
3. **Rate limiting** to prevent API throttling
4. **Retry logic** with exponential backoff for LLM calls
5. **Health check endpoint** for monitoring

**Suggested additions to `llm/model_router.py`**:
```python
class ModelRouter:
    def __init__(self, config_path: str = "config/models.yaml"):
        self.configs = self._load_configs(config_path)
        self.ollama = OllamaClient()
        self.cloud = CloudAPIClient()
        self.usage_stats = {"total_tokens": 0, "cost": 0.0}
        self.health_status = {}
    
    async def generate_with_retry(self, prompt: str, config: ModelConfig,
                                max_retries: int = 3) -> str:
        """Generate with exponential backoff retry logic"""
        for attempt in range(max_retries):
            try:
                result = await self.generate(prompt, config)
                self._track_usage(config, prompt, result)
                return result
            except RateLimitError:
                wait_time = 2 ** attempt
                await asyncio.sleep(wait_time)
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(1)
        
    def _track_usage(self, config: ModelConfig, prompt: str, response: str):
        """Track token usage and cost"""
        if config.type == "remote":
            # Estimate tokens (in production, use actual counts from API)
            tokens = len(prompt.split()) + len(response.split())
            cost = tokens * 0.0001  # Approximate Claude cost
            self.usage_stats["total_tokens"] += tokens
            self.usage_stats["cost"] += cost
    
    async def health_check(self, config: ModelConfig) -> bool:
        """Check if model is available"""
        try:
            if config.type == "local":
                return await self.ollama.health_check(config.name)
            else:
                return await self.cloud.health_check(config.endpoint)
        except:
            return False
```

---

## 11. Risk Mitigation

### Technical Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| **Context overflow** | Medium | High | Subagent spawning, context compression |
| **Hallucinated code** | Medium | Medium | Validation tests, human checkpoints |
| **Security breach** | Low | Critical | Sandboxing, permission models |
| **Model unavailable** | Low | Medium | Fallback model strategy |
| **Memory corruption** | Low | High | Transactional updates, backups |

### Operational Risks

| Risk | Mitigation |
|------|------------|
| **Scope creep** | Strict phase boundaries, backlog management |
| **Performance issues** | Early benchmarking, profiling tools |
| **Integration complexity** | Incremental testing, clear interfaces |
| **Knowledge gaps** | Documentation, expert consultation |

### Contingency Plans

1. **If local LLM fails**: Fall back to cloud API with rate limiting
2. **If sandbox escapes**: Immediate container kill, security audit
3. **If memory corrupts**: Restore from checkpoint, investigate cause
4. **If agent loops**: Implement iteration limits, human intervention

---

## Additional Recommendations

### A. Enhanced Error Handling Strategy

**Add robust error handling throughout**:
```python
# Example: File system tool with error handling
class FileSystemTool:
    def __init__(self, allowed_base_path: str):
        self.allowed_base = Path(allowed_base_path).resolve()
    
    def _validate_path(self, path: str) -> Path:
        """Prevent directory traversal attacks"""
        try:
            resolved = (self.allowed_base / path).resolve()
            if not str(resolved).startswith(str(self.allowed_base)):
                raise PermissionError(f"Path traversal attempt detected: {path}")
            return resolved
        except (OSError, ValueError) as e:
            raise ValueError(f"Invalid path: {path}") from e
    
    def read_file(self, file_path: str) -> str:
        """Read file with comprehensive error handling"""
        try:
            validated = self._validate_path(file_path)
            if not validated.exists():
                raise FileNotFoundError(f"File not found: {file_path}")
            if not validated.is_file():
                raise ValueError(f"Not a file: {file_path}")
            
            with open(validated, 'r', encoding='utf-8') as f:
                return f.read()
        except UnicodeDecodeError:
            raise ValueError(f"File is not text: {file_path}")
        except PermissionError:
            raise PermissionError(f"Permission denied: {file_path}")
```

### B. Streaming Response Support

**Add to Phase 1** for better UX:
```python
# llm/streaming.py
async def generate_stream(self, prompt: str, config: ModelConfig):
    """Generate streaming response for real-time feedback"""
    if config.type == "local":
        async for chunk in self.ollama.stream_generate(prompt, config.name):
            yield chunk
    else:
        async for chunk in self.cloud.stream_generate(prompt, config):
            yield chunk

# Usage in agent
async def executor_node(state: AgentState) -> AgentState:
    """Generate code with streaming"""
    full_response = ""
    
    async for chunk in model_router.generate_stream(
        prompt, 
        state["model_config"]
    ):
        full_response += chunk
        # Emit chunk to UI for real-time display
    
    return {"code_content": full_response}
```

### C. Code-Aware Chunking for ChromaDB

**Improve chunking to respect code boundaries**:
```python
# memory/codebase_memory.py - Enhanced chunking
def _chunk_code_aware(self, content: str, file_type: str = ".py") -> List[dict]:
    """Chunk code while respecting language syntax boundaries"""
    chunks = []
    
    if file_type in [".py", ".js", ".ts", ".java", ".go"]:
        # Split by function/class definitions
        pattern = r'\n(?:def |class |async def |function |const |var )'
    elif file_type in [".rs"]:
        pattern = r'\n(?:fn |struct |impl |pub fn )'
    else:
        # Fallback to line-based chunking
        return [{"content": chunk, "type": "file"} 
                for chunk in self._chunk_code(content)]
    
    parts = re.split(pattern, content)
    
    current_chunk = []
    current_size = 0
    max_size = 2000
    
    for i, part in enumerate(parts):
        if i > 0 and pattern.startswith('\n'):
            # Add back the delimiter
            part = pattern.lstrip('\n') + part
        
        if current_size + len(part) > max_size and current_chunk:
            chunks.append({
                "content": '\n'.join(current_chunk),
                "type": "function_block"
            })
            current_chunk = [part]
            current_size = len(part)
        else:
            current_chunk.append(part)
            current_size += len(part)
    
    if current_chunk:
        chunks.append({
            "content": '\n'.join(current_chunk),
            "type": "function_block"
        })
    
    return chunks
```

### D. Incremental Development Approach

**Consider MVP scope for first release**:
```
MVP Features (Weeks 1-6):
- ✅ Basic file operations (read, write, search)
- ✅ Git integration (status, diff, commit)
- ✅ LLM integration (Ollama + cloud fallback)
- ✅ Session memory (SQLite)
- ✅ Basic LangGraph workflow
- ✅ Claude Skills support (code review, test generation)
- ✅ Docker sandbox
- ❌ MemoryWiki (defer to Phase 3)
- ❌ Multi-agent orchestration (defer to Phase 3)
- ❌ Advanced observability (defer to Phase 4)

Production Features (Weeks 7-10):
- ✅ Full MemoryWiki implementation
- ✅ Multi-agent teams with CrewAI
- ✅ Human-in-the-loop checkpoints
- ✅ Performance optimization
- ✅ Security hardening
```

### E. Testing Strategy Enhancement

**Add comprehensive test coverage**:
```
Test Pyramid:
┌─────────────────────┐
│   E2E Tests         │  ← 20% - Critical user journeys
│   (5-10 tests)      │
├─────────────────────┤
│  Integration Tests   │  ← 30% - Component interactions
│   (20-30 tests)     │
├─────────────────────┤
│   Unit Tests        │  ← 50% - Individual components
│   (50-100 tests)    │
└─────────────────────┘

Test Categories:
- Happy path scenarios
- Error handling and edge cases
- Security boundary tests (path traversal, injection)
- Performance benchmarks
- Multi-model routing tests
```

### F. Monitoring & Observability

**Add observability in Phase 1**:
```python
# observability/metrics.py
from prometheus_client import Counter, Histogram, Gauge
import time

# Metrics
request_count = Counter('agent_requests_total', 'Total requests', ['agent', 'status'])
request_duration = Histogram('agent_request_duration_seconds', 'Request duration')
tokens_used = Counter('agent_tokens_total', 'Tokens used', ['model', 'type'])
active_sessions = Gauge('agent_active_sessions', 'Active sessions')

# Usage in agent
@app.middleware
async def metrics_middleware(request, call_next):
    start = time.time()
    try:
        response = await call_next(request)
        request_count.labels(status="success").inc()
        return response
    except Exception as e:
        request_count.labels(status="error").inc()
        raise
    finally:
        duration = time.time() - start
        request_duration.observe(duration)
```

### G. Backup & Recovery Strategy

**Add to Phase 5**:
```
Backup Strategy:
- Session memory: SQLite WAL mode + hourly snapshots
- Vector memory: ChromaDB persistent storage + daily backups
- Configuration: Git version control + encrypted backups
- Logs: Rotating log files with compression

Recovery Procedures:
1. Database corruption → Restore from latest snapshot
2. Memory loss → Rebuild from git history + file system
3. Configuration error → Roll back to previous version
4. Complete failure → Full system restore from backup image
```

---

## Success Criteria

### Phase 1 Completion (Week 2)
- [ ] Agent can create simple files via CLI
- [ ] MCP server responds to tool calls
- [ ] LLM routing works for local and cloud models
- [ ] Basic tests pass

### Phase 2 Completion (Week 4)
- [ ] Session memory persists across calls
- [ ] Codebase indexed in ChromaDB
- [ ] RAG retrieval returns relevant code
- [ ] MemoryWiki tracks file dependencies

### Phase 3 Completion (Week 6)
- [ ] Multi-agent team collaborates on tasks
- [ ] Subagents spawn with isolated context
- [ ] Results aggregate correctly
- [ ] Human-in-the-loop checkpoints work

### Phase 4 Completion (Week 8)
- [ ] MemoryWiki supports impact analysis
- [ ] Performance benchmarks met
- [ ] Debugging tools functional
- [ ] Documentation complete

### Phase 5 Completion (Week 10)
- [ ] Sandbox prevents unauthorized access
- [ ] Permission model enforced
- [ ] Security audit passed
- [ ] Full test suite green

---

## Next Steps

1. **Review this plan** with team/stakeholders
2. **Adjust scope** based on available resources
3. **Set up project repository** and CI/CD pipeline
4. **Begin Phase 1** with Week 1 tasks
5. **Track progress** using issue tracker and milestones

---

*This implementation plan synthesizes research from `local-agent` and `coding-agent` topics, incorporating Claude Skills patterns, MCP integration, multi-agent orchestration, memory solutions, and security best practices.*

*Status: draft - awaiting team review and approval before Phase 1 initiation*
