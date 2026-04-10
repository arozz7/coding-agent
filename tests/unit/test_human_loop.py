"""Unit tests for human-in-the-loop checkpoints."""
import pytest
from datetime import datetime
from agent.human_loop import (
    HumanInTheLoop,
    CheckpointManager,
    ApprovalLevel,
    CheckpointTrigger,
    create_human_in_the_loop,
)


class TestHumanInTheLoop:
    def test_initialization(self):
        hitl = HumanInTheLoop(auto_approve_safe=True, default_timeout=30)
        
        assert hitl.auto_approve_safe is True
        assert hitl.default_timeout == 30
        assert len(hitl._checkpoints) == 0
    
    def test_register_checkpoint(self):
        hitl = HumanInTheLoop()
        
        cp_id = hitl.register_checkpoint(
            name="test_checkpoint",
            description="Test checkpoint",
            trigger=CheckpointTrigger.ON_START,
            approval_level=ApprovalLevel.CONFIRM,
        )
        
        assert cp_id is not None
        assert cp_id in hitl._checkpoints
        assert hitl._checkpoints[cp_id].name == "test_checkpoint"
    
    def test_should_pause_auto_level(self):
        hitl = HumanInTheLoop()
        
        cp_id = hitl.register_checkpoint(
            name="test",
            description="test",
            trigger=CheckpointTrigger.ON_START,
            approval_level=ApprovalLevel.AUTO,
        )
        
        should_pause = hitl.should_pause(cp_id, {"operation": "read"})
        assert should_pause is False
    
    def test_should_pause_approval_required(self):
        hitl = HumanInTheLoop()
        
        cp_id = hitl.register_checkpoint(
            name="test",
            description="test",
            trigger=CheckpointTrigger.ON_START,
            approval_level=ApprovalLevel.APPROVAL_REQUIRED,
        )
        
        should_pause = hitl.should_pause(cp_id, {"operation": "write"})
        assert should_pause is True
    
    def test_should_pause_confirm_auto_approve_safe(self):
        hitl = HumanInTheLoop(auto_approve_safe=True)
        
        cp_id = hitl.register_checkpoint(
            name="test",
            description="test",
            trigger=CheckpointTrigger.BEFORE_TOOL_CALL,
            approval_level=ApprovalLevel.CONFIRM,
        )
        
        should_pause = hitl.should_pause(cp_id, {"tool": "read_file", "operation": "read"})
        assert should_pause is False
    
    def test_should_pause_confirm_write(self):
        hitl = HumanInTheLoop(auto_approve_safe=True)
        
        cp_id = hitl.register_checkpoint(
            name="test",
            description="test",
            trigger=CheckpointTrigger.BEFORE_TOOL_CALL,
            approval_level=ApprovalLevel.CONFIRM,
        )
        
        should_pause = hitl.should_pause(cp_id, {"tool": "write_file", "operation": "write"})
        assert should_pause is True
    
    def test_request_approval(self):
        hitl = HumanInTheLoop()
        
        cp_id = hitl.register_checkpoint(
            name="test",
            description="test",
            trigger=CheckpointTrigger.ON_START,
            approval_level=ApprovalLevel.APPROVAL_REQUIRED,
        )
        
        approval = hitl.request_approval(cp_id, {"task": "test"})
        
        assert approval is not None
        assert approval.checkpoint_id == cp_id
        assert approval.status == "pending"
    
    def test_approve(self):
        hitl = HumanInTheLoop()
        
        cp_id = hitl.register_checkpoint(
            name="test",
            description="test",
            trigger=CheckpointTrigger.ON_START,
            approval_level=ApprovalLevel.APPROVAL_REQUIRED,
        )
        
        hitl.request_approval(cp_id, {"task": "test"})
        
        result = hitl.approve(cp_id, "Approved")
        
        assert result is True
        status = hitl.get_status(cp_id)
        assert status.status == "approved"
        assert status.response == "Approved"
    
    def test_reject(self):
        hitl = HumanInTheLoop()
        
        cp_id = hitl.register_checkpoint(
            name="test",
            description="test",
            trigger=CheckpointTrigger.ON_START,
            approval_level=ApprovalLevel.APPROVAL_REQUIRED,
        )
        
        hitl.request_approval(cp_id, {"task": "test"})
        
        result = hitl.reject(cp_id, "Not approved")
        
        assert result is True
        status = hitl.get_status(cp_id)
        assert status.status == "rejected"
    
    def test_get_pending(self):
        hitl = HumanInTheLoop()
        
        cp1 = hitl.register_checkpoint("cp1", "test1", CheckpointTrigger.ON_START, ApprovalLevel.APPROVAL_REQUIRED)
        cp2 = hitl.register_checkpoint("cp2", "test2", CheckpointTrigger.ON_START, ApprovalLevel.APPROVAL_REQUIRED)
        
        hitl.request_approval(cp1, {"task": "test"})
        hitl.request_approval(cp2, {"task": "test"})
        hitl.approve(cp1)
        
        pending = hitl.get_pending()
        
        assert len(pending) == 1
        assert pending[0].checkpoint_id == cp2
    
    def test_clear_completed(self):
        hitl = HumanInTheLoop()
        
        cp1 = hitl.register_checkpoint("cp1", "test1", CheckpointTrigger.ON_START, ApprovalLevel.APPROVAL_REQUIRED)
        cp2 = hitl.register_checkpoint("cp2", "test2", CheckpointTrigger.ON_START, ApprovalLevel.APPROVAL_REQUIRED)
        
        hitl.request_approval(cp1, {"task": "test"})
        hitl.request_approval(cp2, {"task": "test"})
        hitl.approve(cp1)
        
        count = hitl.clear_completed()
        
        assert count == 1


class TestCheckpointManager:
    def test_create_standard_checkpoints(self):
        hitl = HumanInTheLoop()
        manager = CheckpointManager(hitl)
        
        manager.create_standard_checkpoints()
        
        assert len(hitl._checkpoints) == 5
    
    def test_create_checkpoint_for_tool(self):
        hitl = HumanInTheLoop()
        manager = CheckpointManager(hitl)
        
        cp_id = manager.create_checkpoint_for_tool("deploy", ApprovalLevel.APPROVAL_REQUIRED)
        
        assert cp_id is not None
        assert cp_id in hitl._checkpoints


class TestCreateHumanInTheLoop:
    def test_create_with_defaults(self):
        hitl, manager = create_human_in_the_loop()
        
        assert hitl is not None
        assert manager is not None
        assert len(hitl._checkpoints) == 5
    
    def test_create_without_standard(self):
        hitl, manager = create_human_in_the_loop(create_standard=False)
        
        assert len(hitl._checkpoints) == 0
    
    def test_custom_timeout(self):
        hitl, manager = create_human_in_the_loop(timeout=120)
        
        assert hitl.default_timeout == 120