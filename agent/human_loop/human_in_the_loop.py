from typing import Dict, Any, Optional, List, Callable
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
import structlog

logger = structlog.get_logger()


class ApprovalLevel(Enum):
    NONE = "none"
    AUTO = "auto"
    CONFIRM = "confirm"
    APPROVAL_REQUIRED = "approval_required"


class CheckpointTrigger(Enum):
    ON_START = "on_start"
    ON_COMPLETION = "on_completion"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
    ON_ERROR = "on_error"
    CUSTOM = "custom"


@dataclass
class Checkpoint:
    id: str
    name: str
    description: str
    trigger: CheckpointTrigger
    approval_level: ApprovalLevel
    conditions: Dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 60
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ApprovalRequest:
    checkpoint_id: str
    checkpoint_name: str
    description: str
    context: Dict[str, Any]
    requested_at: datetime
    status: str = "pending"
    response: Optional[str] = None
    responded_at: Optional[datetime] = None


class HumanInTheLoop:
    def __init__(
        self,
        auto_approve_safe: bool = True,
        default_timeout: int = 60,
    ):
        self.auto_approve_safe = auto_approve_safe
        self.default_timeout = default_timeout
        self._checkpoints: Dict[str, Checkpoint] = {}
        self._pending_approvals: Dict[str, ApprovalRequest] = {}
        self._approval_handlers: Dict[str, Callable] = {}
        self.logger = logger.bind(component="human_in_the_loop")
    
    def register_checkpoint(
        self,
        name: str,
        description: str,
        trigger: CheckpointTrigger,
        approval_level: ApprovalLevel = ApprovalLevel.CONFIRM,
        conditions: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[int] = None,
    ) -> str:
        import uuid
        checkpoint_id = f"cp_{uuid.uuid4().hex[:8]}"
        
        checkpoint = Checkpoint(
            id=checkpoint_id,
            name=name,
            description=description,
            trigger=trigger,
            approval_level=approval_level,
            conditions=conditions or {},
            timeout_seconds=timeout_seconds or self.default_timeout,
        )
        
        self._checkpoints[checkpoint_id] = checkpoint
        self.logger.info(
            "checkpoint_registered",
            id=checkpoint_id,
            name=name,
            trigger=trigger.value,
        )
        
        return checkpoint_id
    
    def register_handler(
        self,
        checkpoint_id: str,
        handler: Callable[[ApprovalRequest], bool],
    ) -> None:
        self._approval_handlers[checkpoint_id] = handler
        self.logger.debug("handler_registered", checkpoint=checkpoint_id)
    
    def should_pause(
        self,
        checkpoint_id: str,
        context: Dict[str, Any],
    ) -> bool:
        checkpoint = self._checkpoints.get(checkpoint_id)
        if not checkpoint:
            return False
        
        if checkpoint.approval_level == ApprovalLevel.AUTO:
            return False
        
        if checkpoint.approval_level == ApprovalLevel.NONE:
            return False
        
        if checkpoint.approval_level == ApprovalLevel.APPROVAL_REQUIRED:
            return True
        
        if checkpoint.approval_level == ApprovalLevel.CONFIRM:
            if self.auto_approve_safe and self._is_safe_operation(context):
                return False
            return True
        
        return False
    
    def _is_safe_operation(self, context: Dict[str, Any]) -> bool:
        safe_operations = {"read", "search", "list", "glob", "grep"}
        
        tool_name = context.get("tool", "").lower()
        operation = context.get("operation", "").lower()
        
        return tool_name in safe_operations or operation in safe_operations
    
    def request_approval(
        self,
        checkpoint_id: str,
        context: Dict[str, Any],
    ) -> ApprovalRequest:
        checkpoint = self._checkpoints.get(checkpoint_id)
        if not checkpoint:
            raise ValueError(f"Unknown checkpoint: {checkpoint_id}")
        
        approval_request = ApprovalRequest(
            checkpoint_id=checkpoint_id,
            checkpoint_name=checkpoint.name,
            description=checkpoint.description,
            context=context,
            requested_at=datetime.utcnow(),
        )
        
        self._pending_approvals[approval_request.checkpoint_id] = approval_request
        
        self.logger.info(
            "approval_requested",
            checkpoint_id=checkpoint_id,
            checkpoint_name=checkpoint.name,
        )
        
        if checkpoint_id in self._approval_handlers:
            handler = self._approval_handlers[checkpoint_id]
            approved = self._call_handler_sync(handler, approval_request)
            if approved:
                self._approve(approval_request.checkpoint_id)
        
        return approval_request
    
    def _call_handler_sync(
        self,
        handler: Callable,
        request: ApprovalRequest,
    ) -> bool:
        try:
            if callable(handler):
                result = handler(request)
                return result
        except Exception as e:
            self.logger.error("handler_error", error=str(e))
        return False
    
    def approve(self, approval_id: str, response: Optional[str] = None) -> bool:
        return self._approve(approval_id, response)
    
    def _approve(self, checkpoint_id: str, response: Optional[str] = None) -> bool:
        if checkpoint_id not in self._pending_approvals:
            return False
        
        approval = self._pending_approvals[checkpoint_id]
        approval.status = "approved"
        approval.response = response
        approval.responded_at = datetime.utcnow()
        
        self.logger.info("approval_granted", checkpoint_id=checkpoint_id)
        return True
    
    def reject(self, approval_id: str, reason: Optional[str] = None) -> bool:
        if approval_id not in self._pending_approvals:
            return False
        
        approval = self._pending_approvals[approval_id]
        approval.status = "rejected"
        approval.response = reason
        approval.responded_at = datetime.utcnow()
        
        self.logger.warning("approval_rejected", checkpoint_id=approval_id, reason=reason)
        return True
    
    def get_pending(self) -> List[ApprovalRequest]:
        return [
            req for req in self._pending_approvals.values()
            if req.status == "pending"
        ]
    
    def get_status(self, checkpoint_id: str) -> Optional[ApprovalRequest]:
        return self._pending_approvals.get(checkpoint_id)
    
    def clear_completed(self) -> int:
        completed = [
            cid for cid, req in self._pending_approvals.items()
            if req.status != "pending"
        ]
        for cid in completed:
            del self._pending_approvals[cid]
        
        self.logger.info("cleared_completed", count=len(completed))
        return len(completed)


class CheckpointManager:
    def __init__(self, hitl: HumanInTheLoop):
        self.hitl = hitl
        self.logger = logger.bind(component="checkpoint_manager")
    
    def create_standard_checkpoints(self) -> None:
        self.hitl.register_checkpoint(
            name="file_write",
            description="Writing files to disk",
            trigger=CheckpointTrigger.BEFORE_TOOL_CALL,
            approval_level=ApprovalLevel.CONFIRM,
            conditions={"tool": "write_file"},
        )
        
        self.hitl.register_checkpoint(
            name="command_execution",
            description="Executing shell commands",
            trigger=CheckpointTrigger.BEFORE_TOOL_CALL,
            approval_level=ApprovalLevel.APPROVAL_REQUIRED,
            conditions={"tool": "execute"},
        )
        
        self.hitl.register_checkpoint(
            name="git_commit",
            description="Committing changes to git",
            trigger=CheckpointTrigger.BEFORE_TOOL_CALL,
            approval_level=ApprovalLevel.APPROVAL_REQUIRED,
            conditions={"tool": "git_commit"},
        )
        
        self.hitl.register_checkpoint(
            name="delete_files",
            description="Deleting files",
            trigger=CheckpointTrigger.BEFORE_TOOL_CALL,
            approval_level=ApprovalLevel.APPROVAL_REQUIRED,
            conditions={"tool": "delete"},
        )
        
        self.hitl.register_checkpoint(
            name="deploy",
            description="Deployment operations",
            trigger=CheckpointTrigger.CUSTOM,
            approval_level=ApprovalLevel.APPROVAL_REQUIRED,
        )
        
        self.logger.info("standard_checkpoints_created", count=5)
    
    def create_checkpoint_for_tool(self, tool_name: str, level: ApprovalLevel) -> str:
        return self.hitl.register_checkpoint(
            name=f"tool_{tool_name}",
            description=f"Checkpoint before {tool_name} execution",
            trigger=CheckpointTrigger.BEFORE_TOOL_CALL,
            approval_level=level,
            conditions={"tool": tool_name},
        )


def create_human_in_the_loop(
    auto_approve_safe: bool = True,
    timeout: int = 60,
    create_standard: bool = True,
) -> tuple[HumanInTheLoop, CheckpointManager]:
    hitl = HumanInTheLoop(auto_approve_safe=auto_approve_safe, default_timeout=timeout)
    manager = CheckpointManager(hitl)
    
    if create_standard:
        manager.create_standard_checkpoints()
    
    return hitl, manager