"""Unit tests for subagent spawner."""
import pytest
from unittest.mock import AsyncMock
from uuid import uuid4

from agent.subagent import SubagentSpawner, SubagentContext, SubagentOrchestrator


class TestSubagentContext:
    def test_initialization(self):
        ctx = SubagentContext(
            id="test_1",
            parent_id="root",
            task="test task",
            created_at=None,
        )
        
        assert ctx.id == "test_1"
        assert ctx.parent_id == "root"
        assert ctx.task == "test task"
        assert ctx.current_depth == 0


class TestSubagentSpawner:
    def test_initialization(self):
        spawner = SubagentSpawner(max_depth=3, max_subagents=5)
        
        assert spawner.max_depth == 3
        assert spawner.max_subagents == 5
        assert spawner.get_active_count() == 0
    
    def test_spawn_subagent(self):
        spawner = SubagentSpawner()
        
        context = spawner.spawn("root", "Implement login")
        
        assert context is not None
        assert context.parent_id == "root"
        assert context.task == "Implement login"
        assert spawner.get_active_count() == 1
    
    def test_max_subagents_limit(self):
        spawner = SubagentSpawner(max_subagents=2)
        
        spawner.spawn("root", "task1")
        spawner.spawn("root", "task2")
        
        with pytest.raises(RuntimeError, match="Max subagents"):
            spawner.spawn("root", "task3")
    
    def test_max_depth_limit(self):
        spawner = SubagentSpawner(max_depth=2)
        
        ctx1 = spawner.spawn("root", "task1")
        ctx2 = spawner.spawn(ctx1.id, "task2")
        
        with pytest.raises(RuntimeError, match="Max depth"):
            spawner.spawn(ctx2.id, "task3")
    
    def test_get_context(self):
        spawner = SubagentSpawner()
        
        context = spawner.spawn("root", "test task")
        retrieved = spawner.get_context(context.id)
        
        assert retrieved is not None
        assert retrieved.id == context.id
    
    def test_get_children(self):
        spawner = SubagentSpawner()
        
        parent = spawner.spawn("root", "parent task")
        spawner.spawn(parent.id, "child1")
        spawner.spawn(parent.id, "child2")
        
        children = spawner.get_children(parent.id)
        
        assert len(children) == 2
    
    def test_get_ancestors(self):
        spawner = SubagentSpawner(max_depth=3)  # explicit depth for 3-level traversal test

        root = spawner.spawn("root", "root task")
        child1 = spawner.spawn(root.id, "child1 task")
        child2 = spawner.spawn(child1.id, "child2 task")
        
        ancestors = spawner.get_ancestors(child2.id)
        
        assert root.id in ancestors
        assert child1.id in ancestors
    
    def test_terminate(self):
        spawner = SubagentSpawner()
        
        context = spawner.spawn("root", "test")
        assert spawner.get_active_count() == 1
        
        result = spawner.terminate(context.id)
        
        assert result is True
        assert spawner.get_active_count() == 0
    
    def test_terminate_branch(self):
        spawner = SubagentSpawner()
        
        parent = spawner.spawn("root", "parent")
        child1 = spawner.spawn(parent.id, "child1")
        child2 = spawner.spawn(parent.id, "child2")
        
        assert spawner.get_active_count() == 3
        
        count = spawner.terminate_branch(parent.id)
        
        assert count == 3
        assert spawner.get_active_count() == 0
    
    def test_get_stats(self):
        spawner = SubagentSpawner(max_depth=3, max_subagents=10)
        
        spawner.spawn("root", "task1")
        spawner.spawn("root", "task2")
        
        stats = spawner.get_stats()
        
        assert stats["active_subagents"] == 2
        assert stats["max_depth_reached"] == 0
        assert stats["max_subagents_limit"] == 10
    
    def test_clear_all(self):
        spawner = SubagentSpawner()
        
        spawner.spawn("root", "task1")
        spawner.spawn("root", "task2")
        
        assert spawner.get_active_count() == 2
        
        spawner.clear_all()
        
        assert spawner.get_active_count() == 0


@pytest.mark.asyncio
class TestIsolatedSubagentExecutor:
    async def test_execute_in_isolation(self):
        from agent.subagent.spawner import IsolatedSubagentExecutor
        
        spawner = SubagentSpawner()
        executor = IsolatedSubagentExecutor(spawner)
        
        async def mock_executor(ctx):
            return {"success": True, "result": "done"}
        
        result = await executor.execute_in_isolation(
            parent_id="root",
            task="test task",
            executor_func=mock_executor,
        )
        
        assert result["success"] is True
        assert "subagent_id" in result
        assert spawner.get_active_count() == 0
    
    async def test_execute_parallel(self):
        from agent.subagent.spawner import IsolatedSubagentExecutor
        
        spawner = SubagentSpawner(max_subagents=10)
        executor = IsolatedSubagentExecutor(spawner)
        
        async def mock_executor(ctx):
            return {"success": True, "result": "done"}
        
        tasks = ["task1", "task2", "task3"]
        
        results = await executor.execute_parallel(
            parent_id="root",
            tasks=tasks,
            executor_func=mock_executor,
        )
        
        assert len(results) == 3
        assert all(r["success"] for r in results)


@pytest.mark.asyncio
class TestSubagentOrchestrator:
    async def test_initialization(self):
        orchestrator = SubagentOrchestrator()
        
        assert orchestrator.root_agent_id is not None
        assert orchestrator.spawner.get_active_count() >= 1
    
    async def test_run_with_subtasks(self):
        orchestrator = SubagentOrchestrator()
        
        async def decompose(task):
            return ["subtask1", "subtask2"]
        
        async def execute(task, ctx):
            return {"success": True, "output": f"done: {task}"}
        
        result = await orchestrator.run_with_subagents(
            task="main task",
            decompose_func=decompose,
            execute_func=execute,
        )
        
        assert result["success"] is True
        assert result["total_subtasks"] == 2
    
    async def test_run_with_no_subtasks(self):
        orchestrator = SubagentOrchestrator()
        
        async def decompose(task):
            return []
        
        async def execute(task, ctx):
            return {"success": True, "result": "direct execution"}
        
        result = await orchestrator.run_with_subagents(
            task="simple task",
            decompose_func=decompose,
            execute_func=execute,
        )
        
        assert result["success"] is True
    
    def test_get_tree(self):
        from agent.subagent.spawner import SubagentSpawner
        orchestrator = SubagentOrchestrator(spawner=SubagentSpawner(max_depth=3))  # 3-level tree test

        spawner = orchestrator.spawner
        child = spawner.spawn(orchestrator.root_agent_id, "child task")
        spawner.spawn(child.id, "grandchild task")
        
        tree = orchestrator.get_tree()
        
        assert tree["id"] == orchestrator.root_agent_id
        assert len(tree["children"]) >= 1