"""Unit tests for PlannerAgent.plan_with_criteria() and PlanResult."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from agent.agents.planner_agent import PlannerAgent, PlanResult


def _make_planner(task_json: str, criteria_json: str = "") -> PlannerAgent:
    router = MagicMock()
    router.get_model.return_value = MagicMock(name="test-model")
    calls: list = []

    async def _generate(prompt, model, **kwargs):
        # First call returns task list; second call returns criteria
        calls.append(prompt)
        if len(calls) == 1:
            return task_json
        return criteria_json or '{"criteria": []}'

    router.generate = _generate
    return PlannerAgent(router)


class TestPlanResult:
    def test_iterable_yields_tasks(self):
        pr = PlanResult(tasks=[{"description": "a", "agent_type": "develop"}], completion_criteria=["c1"])
        items = list(pr)
        assert items == [{"description": "a", "agent_type": "develop"}]

    def test_len_matches_task_count(self):
        pr = PlanResult(tasks=[{"description": "a", "agent_type": "develop"}, {"description": "b", "agent_type": "develop"}])
        assert len(pr) == 2

    def test_index_access(self):
        pr = PlanResult(tasks=[{"description": "t1", "agent_type": "develop"}])
        assert pr[0]["description"] == "t1"

    def test_default_empty_criteria(self):
        pr = PlanResult(tasks=[])
        assert pr.completion_criteria == []


class TestStrategyHintCreation:
    """Regression coverage for the fix that replaced the CREATION strategy's
    single hardcoded web-specific (HTML/CSS/<script>) template — which forced
    every non-trivial build into one file grown via repeated APPEND: — with a
    language-agnostic, per-file-per-responsibility principle."""

    def test_no_hardcoded_html_template(self):
        from agent.agents.planner_agent import PlannerAgent

        planner = PlannerAgent(MagicMock())
        hint = planner._strategy_hint("develop")

        assert "HTML skeleton, CSS, opening <script>" not in hint
        assert "APPEND ONLY" not in hint

    def test_mentions_multiple_languages(self):
        from agent.agents.planner_agent import PlannerAgent

        planner = PlannerAgent(MagicMock())
        hint = planner._strategy_hint("develop")

        # Concrete examples across languages, not just the web-only case —
        # a weak local model needs something to pattern-match, but it must
        # not overfit to one language the way the old prompt did.
        assert "player.js" in hint
        assert ".py" in hint
        assert ".rs" in hint

    def test_one_task_per_file_principle_stated(self):
        from agent.agents.planner_agent import PlannerAgent

        planner = PlannerAgent(MagicMock())
        hint = planner._strategy_hint("develop")

        assert "one" in hint.lower() and "per" in hint.lower()


class TestMaxDevelopTasksCap:
    @pytest.mark.asyncio
    async def test_plan_truncates_at_raised_cap(self):
        """The cap was raised from 6 to accommodate one-task-per-file plans
        for multi-responsibility objectives, but a hard ceiling must still
        exist regardless of what the model returns."""
        from agent.agents.planner_agent import _MAX_DEVELOP_TASKS

        assert _MAX_DEVELOP_TASKS > 6  # raised, not just renamed

        oversized = [
            {"description": f"Write file_{i}.py", "agent_type": "develop"}
            for i in range(_MAX_DEVELOP_TASKS + 5)
        ]
        planner = _make_planner(json.dumps(oversized))

        tasks = await planner.plan("Build a multi-module app", task_type="develop")

        assert len(tasks) == _MAX_DEVELOP_TASKS


class TestPlanWithCriteria:
    @pytest.mark.asyncio
    async def test_returns_plan_result(self):
        planner = _make_planner(
            '[{"description": "Run npm start", "agent_type": "develop"}]',
            '{"criteria": ["command exits 0: npm test", "file exists: src/app.js"]}',
        )
        result = await planner.plan_with_criteria("build a node app", task_type="develop")
        assert isinstance(result, PlanResult)
        assert len(result.tasks) == 1
        assert len(result.completion_criteria) == 2

    @pytest.mark.asyncio
    async def test_criteria_are_strings(self):
        planner = _make_planner(
            '[{"description": "Do stuff", "agent_type": "develop"}]',
            '{"criteria": ["command exits 0: npm test"]}',
        )
        result = await planner.plan_with_criteria("build something", task_type="develop")
        assert all(isinstance(c, str) for c in result.completion_criteria)

    @pytest.mark.asyncio
    async def test_research_task_type_skips_criteria(self):
        planner = _make_planner('[{"description": "Search web", "agent_type": "research"}]')
        result = await planner.plan_with_criteria("research Python", task_type="research")
        assert result.completion_criteria == []

    @pytest.mark.asyncio
    async def test_backward_compat_iteration(self):
        """plan_with_criteria result can be iterated like a plain list."""
        planner = _make_planner(
            '[{"description": "Step 1", "agent_type": "develop"}]',
            '{"criteria": []}',
        )
        result = await planner.plan_with_criteria("obj", task_type="develop")
        descriptions = [t["description"] for t in result]
        assert descriptions == ["Step 1"]

    @pytest.mark.asyncio
    async def test_criteria_capped_at_5(self):
        planner = _make_planner(
            '[{"description": "Do all", "agent_type": "develop"}]',
            '{"criteria": ["c1", "c2", "c3", "c4", "c5", "c6", "c7"]}',
        )
        result = await planner.plan_with_criteria("obj", task_type="develop")
        assert len(result.completion_criteria) <= 5

    @pytest.mark.asyncio
    async def test_malformed_criteria_json_returns_empty(self):
        planner = _make_planner(
            '[{"description": "Do stuff", "agent_type": "develop"}]',
            "Sorry, I cannot generate criteria for this task.",
        )
        result = await planner.plan_with_criteria("obj", task_type="develop")
        assert result.completion_criteria == []

    @pytest.mark.asyncio
    async def test_criteria_prompt_bans_markdown_file_contains(self):
        """Regression test: completion criteria like 'file contains:
        NEXT_STEPS.md:Prioritized' are trivially satisfiable by appending a
        single token, proving nothing about real progress (see
        logs/api-20260705-233402.log). The criteria-generation system prompt
        must forbid 'file contains' criteria against markdown files, mirroring
        the rule RequirementsExtractor already applies to acceptance criteria.
        """
        router = MagicMock()
        router.get_model.return_value = MagicMock(name="test-model")
        captured: dict = {}

        async def _generate(prompt, model, **kwargs):
            if "system_prompt" in kwargs and "criteria" in kwargs["system_prompt"].lower():
                captured["system_prompt"] = kwargs["system_prompt"]
                return '{"criteria": []}'
            return '[{"description": "Do stuff", "agent_type": "develop"}]'

        router.generate = _generate
        planner = PlannerAgent(router)
        await planner.plan_with_criteria("update NEXT_STEPS.md", task_type="develop")

        assert "system_prompt" in captured, "criteria system prompt was never generated"
        assert "markdown" in captured["system_prompt"].lower()
        assert ".md" in captured["system_prompt"]

    @pytest.mark.asyncio
    async def test_find_criterion_rewritten_to_portable_file_exists(self):
        """Regression test for the same class of bug normalize_criterion
        exists to fix (see logs/api-20260709-111051.log): completion
        criteria must not depend on 'find ... | grep' surviving on Windows.
        """
        planner = _make_planner(
            '[{"description": "Do stuff", "agent_type": "develop"}]',
            '{"criteria": ["command exits 0: find src -type f -name \'*.py\' | grep -q ."]}',
        )
        result = await planner.plan_with_criteria("obj", task_type="develop")
        assert result.completion_criteria == ["file exists: src/**/*.py"]
