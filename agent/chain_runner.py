"""Declarative agent chain runner.

Reads agent-chain.yaml from the workspace root and executes named pipelines.
Each step passes $INPUT (previous step's output) and $ORIGINAL (user's request)
to the next agent — modelled after the pi-vs-claude-code agent-chain pattern.

Example agent-chain.yaml:

  plan-build-review:
    description: "Plan, implement, and review"
    steps:
      - agent: planner
        prompt: "Plan the implementation for: $INPUT"
      - agent: builder
        prompt: "Implement the following plan:\\n\\n$INPUT"
      - agent: reviewer
        prompt: "Review this implementation:\\n\\n$INPUT"

Usage:
  runner = ChainRunner(orchestrator)
  result = await runner.run("plan-build-review", "Add auth to the API", session_id, on_phase=cb)
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
import yaml

logger = structlog.get_logger()

# Agent type aliases — maps chain YAML names to orchestrator agent types.
_AGENT_ALIASES: Dict[str, str] = {
    "builder":  "develop",
    "scout":    "research",
    "planner":  "architect",
    "bowser":   "develop",   # browser tasks fall to developer w/ screenshot
}


class ChainRunner:
    """Executes named agent chains defined in agent-chain.yaml."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.logger = logger.bind(component="chain_runner")

    def _load_chains(self, workspace_path: str) -> Dict[str, Any]:
        """Load agent-chain.yaml from workspace root. Returns {} if not found."""
        chain_file = Path(workspace_path) / "agent-chain.yaml"
        if not chain_file.exists():
            # Fall back to built-in chains
            return self._builtin_chains()
        try:
            with open(chain_file, encoding="utf-8") as fh:
                return yaml.safe_load(fh) or {}
        except Exception as e:
            self.logger.warning("chain_yaml_load_failed", error=str(e))
            return self._builtin_chains()

    def _builtin_chains(self) -> Dict[str, Any]:
        """Built-in chains available even without an agent-chain.yaml file."""
        return {
            "plan-build-review": {
                "description": "Plan, implement, and review — the standard development cycle",
                "steps": [
                    {"agent": "planner",  "prompt": "Create a detailed implementation plan for: $INPUT"},
                    {"agent": "builder",  "prompt": "Implement the following plan:\n\n$INPUT\n\nOriginal request: $ORIGINAL"},
                    {"agent": "reviewer", "prompt": "Review this implementation for bugs, style, and correctness:\n\n$INPUT\n\nOriginal request: $ORIGINAL"},
                ],
            },
            "plan-build": {
                "description": "Plan then build — fast two-step without review",
                "steps": [
                    {"agent": "planner", "prompt": "Plan the implementation for: $INPUT"},
                    {"agent": "builder", "prompt": "Based on this plan, implement:\n\n$INPUT\n\nOriginal request: $ORIGINAL"},
                ],
            },
            "scout-flow": {
                "description": "Triple-scout deep recon — explore, validate, verify",
                "steps": [
                    {"agent": "scout", "prompt": "Explore the codebase and investigate: $INPUT\n\nReport findings with structure, key files, and patterns."},
                    {"agent": "scout", "prompt": "Validate and cross-check this analysis. Look for anything missed:\n\n$INPUT\n\nOriginal: $ORIGINAL"},
                    {"agent": "scout", "prompt": "Final review pass. Verify accuracy and add missing details:\n\n$INPUT\n\nOriginal: $ORIGINAL"},
                ],
            },
            "plan-review-plan": {
                "description": "Iterative planning — plan, critique, then refine",
                "steps": [
                    {"agent": "planner",      "prompt": "Create a detailed implementation plan for: $INPUT"},
                    {"agent": "plan-reviewer","prompt": "Critically review this plan. Challenge assumptions, find gaps:\n\n$INPUT\n\nOriginal: $ORIGINAL"},
                    {"agent": "planner",      "prompt": "Revise your plan based on this critique. Address every issue:\n\nOriginal: $ORIGINAL\n\nCritique:\n$INPUT"},
                ],
            },
            "full-review": {
                "description": "End-to-end pipeline — scout, plan, build, review",
                "steps": [
                    {"agent": "scout",    "prompt": "Explore the codebase and identify relevant context for: $INPUT"},
                    {"agent": "planner",  "prompt": "Based on this analysis, create a plan:\n\n$INPUT\n\nOriginal: $ORIGINAL"},
                    {"agent": "builder",  "prompt": "Implement this plan:\n\n$INPUT\n\nOriginal: $ORIGINAL"},
                    {"agent": "reviewer", "prompt": "Review this implementation:\n\n$INPUT\n\nOriginal: $ORIGINAL"},
                ],
            },
            "secure-build": {
                "description": "Plan, build, then run a security audit",
                "steps": [
                    {"agent": "planner",  "prompt": "Plan the implementation for: $INPUT"},
                    {"agent": "builder",  "prompt": "Implement the following plan:\n\n$INPUT\n\nOriginal: $ORIGINAL"},
                    {"agent": "red-team", "prompt": "Security audit the implementation above:\n\n$INPUT\n\nOriginal: $ORIGINAL"},
                ],
            },
        }

    def list_chains(self, workspace_path: str) -> List[Dict[str, str]]:
        """Return [{name, description}, ...] for all available chains."""
        chains = self._load_chains(workspace_path)
        return [
            {"name": name, "description": cfg.get("description", "")}
            for name, cfg in chains.items()
        ]

    async def run(
        self,
        chain_name: str,
        user_input: str,
        session_id: str,
        on_phase: Optional[Callable[[str], None]] = None,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a named chain and return the final step's result.

        Args:
            chain_name: Key from agent-chain.yaml (e.g. "plan-build-review")
            user_input: The user's original request ($ORIGINAL and first $INPUT)
            session_id: Session to run all steps under
            on_phase: Optional progress callback
            job_id: Optional job ID for task tracking
        """
        workspace = self.orchestrator.workspace_path
        chains = self._load_chains(workspace)

        if chain_name not in chains:
            available = ", ".join(chains.keys())
            return {
                "success": False,
                "error": f"Chain '{chain_name}' not found. Available: {available}",
            }

        chain = chains[chain_name]
        steps: List[Dict[str, str]] = chain.get("steps", [])
        if not steps:
            return {"success": False, "error": f"Chain '{chain_name}' has no steps"}

        def _emit(label: str) -> None:
            if on_phase:
                try:
                    on_phase(label)
                except Exception:
                    pass

        _emit(f"chain:{chain_name}:starting")
        self.logger.info("chain_started", name=chain_name, steps=len(steps), input=user_input[:80])

        current_input = user_input
        all_responses: List[str] = []
        all_files: List[str] = []
        final_result: Dict[str, Any] = {}

        for i, step in enumerate(steps):
            agent_name: str = step.get("agent", "develop")
            prompt_template: str = step.get("prompt", "$INPUT")

            # Resolve $INPUT and $ORIGINAL in the step prompt
            step_task = (
                prompt_template
                .replace("$INPUT", current_input)
                .replace("$ORIGINAL", user_input)
            )

            # Map chain agent names to orchestrator agent types
            agent_type = _AGENT_ALIASES.get(agent_name, agent_name)
            # Handle "plan-reviewer" → built-in plan reviewer path
            if agent_name == "plan-reviewer":
                agent_type = "architect"  # architect agent handles plan critique tasks

            step_label = f"chain:{chain_name}:step:{i + 1}/{len(steps)}:{agent_name}"
            _emit(step_label)
            self.logger.info("chain_step", name=chain_name, step=i + 1, agent=agent_name)

            try:
                result = await self.orchestrator._run_specialized_agent(
                    step_task,
                    agent_type,
                    session_id,
                    on_phase=on_phase,
                    job_id=None,
                    _direct=True,
                )
            except Exception as exc:
                self.logger.error("chain_step_failed", step=i + 1, error=str(exc))
                return {
                    "success": False,
                    "error": f"Chain '{chain_name}' failed at step {i + 1} ({agent_name}): {exc}",
                    "partial_responses": all_responses,
                }

            if not result.get("success"):
                error = result.get("error", "agent failed")
                self.logger.warning("chain_step_error", step=i + 1, error=error)
                # Continue with the error text as next input so later steps can recover
                current_input = f"[Step {i + 1} failed: {error}]\n\n{current_input}"
            else:
                response_text = result.get("response", "")
                current_input = response_text
                all_responses.append(f"**Step {i + 1} [{agent_name}]:**\n\n{response_text}")
                all_files.extend(result.get("files_created", []))
                final_result = result

        _emit(f"chain:{chain_name}:complete")
        self.logger.info("chain_complete", name=chain_name, steps_run=len(steps))

        combined = "\n\n---\n\n".join(all_responses)
        seen: set = set()
        unique_files = [f for f in all_files if not (f in seen or seen.add(f))]

        return {
            "success": True,
            "response": combined,
            "files_created": unique_files,
            "chain_name": chain_name,
            "steps_run": len(steps),
            "final_step_result": final_result,
        }
