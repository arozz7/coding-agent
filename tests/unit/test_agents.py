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

    def test_extract_file_writes_ignores_prose_mention_of_file(self):
        # Regression: an unbounded DOTALL path group used to swallow the real
        # FILE: line when the model's own prose mentioned "FILE:" first.
        from agent.agents.architect_agent import ArchitectRole

        role = ArchitectRole()
        response = (
            "I'll write this as a FILE: rather than editing in place.\n\n"
            "FILE: docs/ARCHITECTURE.md\n"
            "```markdown\n# Architecture\n```\n"
        )
        matches = role._extract_file_writes(response)
        assert matches == [("docs/ARCHITECTURE.md", "# Architecture")]


class TestDeveloperRole:
    @pytest.mark.asyncio
    async def test_run_shell_blocks_routes_powershell_script_through_temp_file(self):
        # Regression: logs/api-20260819-201238.log — a PowerShell block
        # assigning $root/$log/$err and calling Start-Process was split
        # naively by newline into per-line "commands", each failing with
        # WinError 2. Such a block must be written to a temp .ps1 and
        # invoked as one powershell -File call instead.
        from agent.agents.developer_agent import DeveloperRole

        role = DeveloperRole()
        response = (
            "```shell\n"
            '$root = "J:\\Projects\\agent-workspace\\payment-tracker"\n'
            '$log  = "$root\\tauri-launch.log"\n'
            'Start-Process -FilePath "cargo" -ArgumentList "run"\n'
            "```\n"
        )

        tool_executor = Mock()
        tool_executor.execute = AsyncMock(return_value="ok")

        all_outputs, failed_outputs = await role._run_shell_blocks(response, tool_executor)

        calls = tool_executor.execute.call_args_list
        write_calls = [c for c in calls if c.args[0] == "file_write"]
        shell_calls = [c for c in calls if c.args[0] == "shell"]

        assert len(write_calls) == 1
        assert write_calls[0].args[1]["path"].endswith(".ps1")
        assert "Start-Process" in write_calls[0].args[1]["content"]

        assert len(shell_calls) == 1
        shell_cmd = shell_calls[0].args[1]["command"]
        assert shell_cmd.startswith("powershell -NoProfile -ExecutionPolicy Bypass -File ")
        # No quotes around the path — shell_tool.py resolves this command via
        # shlex.split(posix=False) + subprocess.Popen(list, shell=False),
        # which passes argv literally with no shell to strip quote chars.
        assert '"' not in shell_cmd
        assert failed_outputs == []

    @pytest.mark.asyncio
    async def test_run_shell_blocks_plain_commands_still_split_per_line(self):
        from agent.agents.developer_agent import DeveloperRole

        role = DeveloperRole()
        response = "```shell\nnpm install\nnpm run build\n```\n"

        tool_executor = Mock()
        tool_executor.execute = AsyncMock(return_value="ok")

        await role._run_shell_blocks(response, tool_executor)

        shell_cmds = [
            c.args[1]["command"] for c in tool_executor.execute.call_args_list if c.args[0] == "shell"
        ]
        assert shell_cmds == ["npm install", "npm run build"]

    @pytest.mark.asyncio
    async def test_run_shell_blocks_skips_block_with_leaked_non_shell_content(self):
        # Regression: logs/api-20260822-210550.log 02:30:33-34 — an unclosed
        # ```shell fence let the non-greedy match swallow a REPLACE: block
        # and bare JS statements, each executed as a shell command and
        # failing with WinError 2. Such a block must be skipped, not run.
        from agent.agents.developer_agent import DeveloperRole

        role = DeveloperRole()
        role.logger = Mock()
        response = (
            "```shell\n"
            "REPLACE: index.html 109-112\n"
            "<<<\n"
            "updateHud();\n"
            ">>>\n"
            "```\n"
        )

        tool_executor = Mock()
        tool_executor.execute = AsyncMock(return_value="ok")

        all_outputs, failed_outputs = await role._run_shell_blocks(response, tool_executor)

        shell_calls = [c for c in tool_executor.execute.call_args_list if c.args[0] == "shell"]
        assert shell_calls == []
        assert all_outputs == []
        assert failed_outputs == []

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
    def test_extract_file_writes_ignores_prose_mention_of_file(self):
        # Regression: an unbounded DOTALL path group used to swallow the real
        # FILE: line when the model's own prose mentioned "FILE:" first.
        from agent.agents.tester_agent import TesterRole

        role = TesterRole()
        response = (
            "I'll add this as a new FILE: for the login test.\n\n"
            "FILE: tests/test_login.py\n"
            "```python\ndef test_login():\n    pass\n```\n"
        )
        matches = role._extract_file_writes(response)
        assert matches == [("tests/test_login.py", "def test_login():\n    pass")]

    @pytest.mark.asyncio
    async def test_tester_execute_does_not_override_model_router_timeout(self):
        # Regression guard, updated: base_agent:tester used to hit the
        # model_router default 600s timeout (logs/api-20260819-201238.log)
        # while developer_agent's calls (already raised to 1500s) did not.
        # The fix moved the timeout budget onto ModelConfig.timeout_secs
        # (see tests/unit/test_model_router_timeout.py), so tester_agent
        # should no longer pass its own timeout override — it must fall
        # through to the model's declared budget like every other role.
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
        await role.execute(context)

        mock_router.generate.assert_called_once()
        assert "timeout" not in mock_router.generate.call_args.kwargs

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