from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime, timezone
import asyncio
import structlog
from uuid import uuid4

logger = structlog.get_logger()


@dataclass
class SubagentContext:
    id: str
    parent_id: str
    task: str
    created_at: datetime
    tools: List[str] = field(default_factory=list)
    max_depth: int = 2
    current_depth: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


class SubagentSpawner:
    def __init__(
        self,
        max_depth: int = 2,
        max_subagents: int = 5,
        isolation_mode: bool = True,
    ):
        self.max_depth = max_depth
        self.max_subagents = max_subagents
        self.isolation_mode = isolation_mode
        self._active_subagents: Dict[str, SubagentContext] = {}
        self._parent_map: Dict[str, str] = {}
        self.logger = logger.bind(component="subagent_spawner")
    
    def spawn(
        self,
        parent_id: str,
        task: str,
        tools: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SubagentContext:
        if len(self._active_subagents) >= self.max_subagents:
            raise RuntimeError(f"Max subagents ({self.max_subagents}) reached")
        
        parent_ctx = self._active_subagents.get(parent_id)
        current_depth = 0
        if parent_ctx:
            current_depth = parent_ctx.current_depth + 1
            if current_depth >= self.max_depth:
                raise RuntimeError(f"Max depth ({self.max_depth}) reached")
        
        subagent_id = f"subagent_{uuid4().hex[:8]}"
        
        context = SubagentContext(
            id=subagent_id,
            parent_id=parent_id,
            task=task,
            created_at=datetime.now(timezone.utc),
            tools=tools or [],
            max_depth=self.max_depth,
            current_depth=current_depth,
            metadata=metadata or {},
        )
        
        self._active_subagents[subagent_id] = context
        self._parent_map[subagent_id] = parent_id
        
        self.logger.info(
            "subagent_spawned",
            subagent_id=subagent_id,
            parent_id=parent_id,
            depth=current_depth,
            task_preview=task[:50],
        )
        
        return context
    
    def get_context(self, subagent_id: str) -> Optional[SubagentContext]:
        return self._active_subagents.get(subagent_id)
    
    def get_children(self, parent_id: str) -> List[SubagentContext]:
        return [
            ctx for ctx in self._active_subagents.values()
            if ctx.parent_id == parent_id
        ]
    
    def get_ancestors(self, subagent_id: str) -> List[str]:
        ancestors = []
        current_id = subagent_id
        
        while current_id in self._parent_map:
            parent_id = self._parent_map[current_id]
            ancestors.append(parent_id)
            current_id = parent_id
        
        return ancestors
    
    def terminate(self, subagent_id: str) -> bool:
        if subagent_id not in self._active_subagents:
            return False
        
        children = self.get_children(subagent_id)
        for child in children:
            self.terminate(child.id)
        
        del self._active_subagents[subagent_id]
        
        if subagent_id in self._parent_map:
            del self._parent_map[subagent_id]
        
        self.logger.info("subagent_terminated", subagent_id=subagent_id)
        return True
    
    def terminate_branch(self, root_id: str) -> int:
        count = 0
        children = self.get_children(root_id)
        
        for child in children:
            count += self.terminate_branch(child.id)
        
        if self.terminate(root_id):
            count += 1
        
        return count
    
    def get_active_count(self) -> int:
        return len(self._active_subagents)
    
    def get_stats(self) -> Dict[str, Any]:
        depths = [ctx.current_depth for ctx in self._active_subagents.values()]
        
        return {
            "active_subagents": len(self._active_subagents),
            "max_depth_reached": max(depths) if depths else 0,
            "max_subagents_limit": self.max_subagents,
            "isolation_mode": self.isolation_mode,
        }
    
    def clear_all(self) -> None:
        count = len(self._active_subagents)
        self._active_subagents.clear()
        self._parent_map.clear()
        self.logger.info("all_subagents_cleared", count=count)


class IsolatedSubagentExecutor:
    def __init__(self, spawner: SubagentSpawner):
        self.spawner = spawner
        self.logger = logger.bind(component="isolated_executor")
    
    async def execute_in_isolation(
        self,
        parent_id: str,
        task: str,
        executor_func,
        tools: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        context = self.spawner.spawn(
            parent_id=parent_id,
            task=task,
            tools=tools,
            metadata={"isolation": self.spawner.isolation_mode},
        )
        
        try:
            result = await executor_func(context)
            
            self.logger.info(
                "subagent_completed",
                subagent_id=context.id,
                success=result.get("success", False),
            )
            
            return {
                "success": True,
                "subagent_id": context.id,
                "result": result,
                "depth": context.current_depth,
            }
            
        except Exception as e:
            self.logger.error(
                "subagent_failed",
                subagent_id=context.id,
                error=str(e),
            )
            
            return {
                "success": False,
                "subagent_id": context.id,
                "error": str(e),
                "depth": context.current_depth,
            }
        
        finally:
            self.spawner.terminate(context.id)
    
    async def execute_parallel(
        self,
        parent_id: str,
        tasks: List[str],
        executor_func,
    ) -> List[Dict[str, Any]]:
        results = await asyncio.gather(
            *[
                self.execute_in_isolation(parent_id, task, executor_func)
                for task in tasks
            ],
            return_exceptions=True,
        )
        
        return [
            r if isinstance(r, dict) else {"success": False, "error": str(r)}
            for r in results
        ]


class SubagentOrchestrator:
    def __init__(self, spawner: Optional[SubagentSpawner] = None):
        self.spawner = spawner or SubagentSpawner()
        self.executor = IsolatedSubagentExecutor(self.spawner)
        self.root_agent_id = f"root_{uuid4().hex[:8]}"
        self.logger = logger.bind(component="subagent_orchestrator")
        
        self.spawner._active_subagents[self.root_agent_id] = SubagentContext(
            id=self.root_agent_id,
            parent_id="",
            task="root",
            created_at=datetime.now(timezone.utc),
            current_depth=0,
        )
    
    async def run_with_subagents(
        self,
        task: str,
        decompose_func,
        execute_func,
        max_iterations: int = 3,
    ) -> Dict[str, Any]:
        self.logger.info("orchestrator_start", task=task[:100])
        
        subtasks = await decompose_func(task)
        
        if not subtasks:
            return await execute_func(task, None)
        
        results = []
        for subtask in subtasks:
            result = await self.executor.execute_in_isolation(
                parent_id=self.root_agent_id,
                task=subtask,
                executor_func=lambda ctx: execute_func(subtask, ctx),
            )
            results.append(result)
        
        aggregated = self._aggregate_results(results)
        
        self.logger.info(
            "orchestrator_complete",
            subtasks=len(subtasks),
            successful=sum(1 for r in results if r.get("success")),
        )
        
        return aggregated
    
    def _aggregate_results(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        successful = [r for r in results if r.get("success")]
        failed = [r for r in results if not r.get("success")]
        
        return {
            "success": len(successful) > 0,
            "total_subtasks": len(results),
            "successful": len(successful),
            "failed": len(failed),
            "results": results,
            "stats": self.spawner.get_stats(),
        }
    
    def get_tree(self, agent_id: Optional[str] = None) -> Dict[str, Any]:
        if agent_id is None:
            agent_id = self.root_agent_id
        
        context = self.spawner.get_context(agent_id)
        if not context:
            return {}
        
        children = self.spawner.get_children(agent_id)
        
        return {
            "id": context.id,
            "task": context.task,
            "depth": context.current_depth,
            "created_at": context.created_at.isoformat(),
            "children": [self.get_tree(child.id) for child in children],
        }