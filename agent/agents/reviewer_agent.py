from typing import Dict, Any, Optional
from agent.agents.base_agent import AgentRole


class ReviewerRole(AgentRole):
    def __init__(self, code_analyzer=None, file_system_tool=None):
        super().__init__(
            name="reviewer",
            description="Reviews code for quality, security, and best practices",
        )
        self.code_analyzer = code_analyzer
        self.file_system_tool = file_system_tool
    
    def get_system_prompt(self) -> str:
        return """You are an expert code reviewer. Your role is to:
- Review code for quality and correctness
- Identify potential bugs and security issues
- Ensure adherence to best practices
- Suggest improvements
- Verify test coverage

When files are provided, analyze them programmatically and review based on:
- Code structure and organization
- Function/class definitions
- Imports and dependencies
- Syntax errors
- Potential issues

Focus on:
- Code smells and anti-patterns
- Security vulnerabilities
- Performance issues
- Maintainability
- Test quality"""

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "")
        code = context.get("code", "")
        file_path = context.get("file_path", "")
        
        analysis_result = None
        if file_path and self.code_analyzer:
            try:
                analysis_result = self.code_analyzer.analyze_file(file_path)
                self.logger.info("file_analyzed", path=file_path, success=analysis_result.get("success"))
            except Exception as e:
                self.logger.error("analysis_failed", path=file_path, error=str(e))
        
        prompt = f"""{self.get_system_prompt()}

Original Task: {task}
"""
        
        if analysis_result and analysis_result.get("success"):
            prompt += f"""
File Analysis:
- Functions: {len(analysis_result.get("functions", []))}
- Classes: {len(analysis_result.get("classes", []))}
- Imports: {analysis_result.get("imports", [])}
- Dependencies: {analysis_result.get("dependencies", [])}
- Total lines: {analysis_result.get("total_lines", 0)}
"""
        
        if code:
            prompt += f"""
Code to Review:
```
{code}
```
"""
        
        prompt += """
Provide a detailed review with:
1. Issues found (severity: critical/high/medium/low)
2. Specific line numbers and suggestions
3. Security concerns
4. Performance recommendations
5. Overall code quality score (1-10)

Format as a structured review."""
        model_router = context.get("model_router")
        
        if not model_router:
            return {"success": False, "error": "model_router not available"}
        
        model = model_router.get_model("coding")
        if not model:
            return {"success": False, "error": "No coding model configured"}
        
        response = await model_router.generate(prompt, model)
        
        issues_count = 0
        if analysis_result and analysis_result.get("success"):
            issues_count = len(analysis_result.get("functions", [])) + len(analysis_result.get("classes", []))
        
        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "analysis": analysis_result,
            "issues_found": issues_count,
        }


class ReviewerAgent:
    def __init__(self, model_router, tools=None, code_analyzer=None, file_system_tool=None):
        from agent.agents.base_agent import BaseAgent
        role = ReviewerRole(code_analyzer, file_system_tool)
        self.base = BaseAgent(role, model_router, tools)
        self.code_analyzer = code_analyzer
        self.file_system_tool = file_system_tool
    
    async def run(self, task: str, context: Dict[str, Any] = None):
        if context is None:
            context = {}
        if self.code_analyzer and "code_analyzer" not in context:
            context["code_analyzer"] = self.code_analyzer
        if self.file_system_tool and "file_system_tool" not in context:
            context["file_system_tool"] = self.file_system_tool
        return await self.base.run(task, context)