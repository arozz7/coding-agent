"""Unit tests for agent roles."""
import pytest
from unittest.mock import Mock, AsyncMock, patch


class TestAgentRole:
    def test_architect_role_initialization(self):
        from agent.agents.architect_agent import ArchitectRole
        
        role = ArchitectRole()
        assert role.name == "architect"
        assert role.description is not None
    
    def test_developer_role_initialization(self):
        from agent.agents.developer_agent import DeveloperRole
        
        role = DeveloperRole()
        assert role.name == "developer"
        assert role.description is not None
    
    def test_reviewer_role_initialization(self):
        from agent.agents.reviewer_agent import ReviewerRole
        
        role = ReviewerRole()
        assert role.name == "reviewer"
        assert role.description is not None
    
    def test_tester_role_initialization(self):
        from agent.agents.tester_agent import TesterRole
        
        role = TesterRole()
        assert role.name == "tester"
        assert role.description is not None


class TestArchitectRole:
    @pytest.mark.asyncio
    async def test_architect_execute(self):
        from agent.agents.architect_agent import ArchitectRole
        
        role = ArchitectRole()
        
        mock_router = Mock()
        mock_model = Mock()
        mock_router.get_model.return_value = mock_model
        mock_router.generate = AsyncMock(return_value="Architecture design")
        
        context = {
            "task": "Build a web app",
            "model_router": mock_router,
        }
        
        result = await role.execute(context)
        
        assert result["success"] == True
        assert result["role"] == "architect"
        mock_router.generate.assert_called_once()


class TestDeveloperRole:
    @pytest.mark.asyncio
    async def test_developer_execute(self):
        from agent.agents.developer_agent import DeveloperRole
        
        role = DeveloperRole()
        
        mock_router = Mock()
        mock_model = Mock()
        mock_router.get_model.return_value = mock_model
        mock_router.generate = AsyncMock(return_value="Implementation code")
        
        context = {
            "task": "Implement login",
            "model_router": mock_router,
        }
        
        result = await role.execute(context)
        
        assert result["success"] == True
        assert result["role"] == "developer"
    
    @pytest.mark.asyncio
    async def test_developer_with_architecture_context(self):
        from agent.agents.developer_agent import DeveloperRole
        
        role = DeveloperRole()
        
        mock_router = Mock()
        mock_model = Mock()
        mock_router.get_model.return_value = mock_model
        mock_router.generate = AsyncMock(return_value="Implementation")
        
        context = {
            "task": "Implement login",
            "architecture": "Use MVC pattern",
            "model_router": mock_router,
        }
        
        result = await role.execute(context)
        
        assert result["success"] == True


class TestReviewerRole:
    @pytest.mark.asyncio
    async def test_reviewer_execute(self):
        from agent.agents.reviewer_agent import ReviewerRole
        
        role = ReviewerRole()
        
        mock_router = Mock()
        mock_model = Mock()
        mock_router.get_model.return_value = mock_model
        mock_router.generate = AsyncMock(return_value="Code review findings")
        
        context = {
            "task": "Review login code",
            "code": "def login(): pass",
            "model_router": mock_router,
        }
        
        result = await role.execute(context)
        
        assert result["success"] == True
        assert result["role"] == "reviewer"


class TestTesterRole:
    @pytest.mark.asyncio
    async def test_tester_execute_python(self):
        from agent.agents.tester_agent import TesterRole
        
        role = TesterRole()
        
        mock_router = Mock()
        mock_model = Mock()
        mock_router.get_model.return_value = mock_model
        mock_router.generate = AsyncMock(return_value="Test code")
        
        context = {
            "task": "Write tests for login",
            "code": "def login(): pass",
            "language": "python",
            "model_router": mock_router,
        }
        
        result = await role.execute(context)
        
        assert result["success"] == True
        assert result["role"] == "tester"
        assert result["language"] == "python"


class TestBaseAgent:
    def test_base_agent_initialization(self):
        from agent.agents.base_agent import BaseAgent, AgentRole
        
        class MockRole(AgentRole):
            def get_system_prompt(self):
                return "mock prompt"
            
            async def execute(self, context):
                return {"success": True}
        
        mock_role = MockRole("test", "test role")
        mock_router = Mock()
        
        agent = BaseAgent(mock_role, mock_router)
        
        assert agent.role == mock_role
        assert agent.model_router == mock_router
        assert agent.tools == []
    
    def test_add_tool(self):
        from agent.agents.base_agent import BaseAgent, AgentRole
        
        class MockRole(AgentRole):
            def get_system_prompt(self):
                return "mock"
            
            async def execute(self, context):
                return {"success": True}
        
        mock_role = MockRole("test", "test")
        mock_router = Mock()
        
        agent = BaseAgent(mock_role, mock_router)
        
        class MockTool:
            pass
        
        tool = MockTool()
        agent.add_tool(tool)
        
        assert len(agent.tools) == 1
    
    def test_remove_tool(self):
        from agent.agents.base_agent import BaseAgent, AgentRole
        
        class MockRole(AgentRole):
            def get_system_prompt(self):
                return "mock"
            
            async def execute(self, context):
                return {"success": True}
        
        mock_role = MockRole("test", "test")
        mock_router = Mock()
        
        agent = BaseAgent(mock_role, mock_router)
        
        class MockTool:
            pass
        
        agent.add_tool(MockTool())
        assert len(agent.tools) == 1
        
        agent.remove_tool("MockTool")
        assert len(agent.tools) == 0


class TestAgentClasses:
    @pytest.mark.asyncio
    async def test_architect_agent_run(self):
        from agent.agents.architect_agent import ArchitectAgent
        
        mock_router = Mock()
        mock_router.get_model = Mock(return_value=Mock(generate=AsyncMock(return_value="Design")))
        
        agent = ArchitectAgent(mock_router)
        result = await agent.run("Design a system")
        
        assert result is not None
    
    @pytest.mark.asyncio
    async def test_developer_agent_run(self):
        from agent.agents.developer_agent import DeveloperAgent
        
        mock_router = Mock()
        mock_router.get_model = Mock(return_value=Mock(generate=AsyncMock(return_value="Code")))
        
        agent = DeveloperAgent(mock_router)
        result = await agent.run("Implement feature")
        
        assert result is not None
    
    @pytest.mark.asyncio
    async def test_reviewer_agent_run(self):
        from agent.agents.reviewer_agent import ReviewerAgent
        
        mock_router = Mock()
        mock_router.get_model = Mock(return_value=Mock(generate=AsyncMock(return_value="Review")))
        
        agent = ReviewerAgent(mock_router)
        result = await agent.run("Review code")
        
        assert result is not None
    
    @pytest.mark.asyncio
    async def test_tester_agent_run(self):
        from agent.agents.tester_agent import TesterAgent
        
        mock_router = Mock()
        mock_router.get_model = Mock(return_value=Mock(generate=AsyncMock(return_value="Tests")))
        
        agent = TesterAgent(mock_router)
        result = await agent.run("Write tests")
        
        assert result is not None