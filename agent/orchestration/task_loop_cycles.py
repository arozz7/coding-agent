"""Criterion/acceptance fix-cycle helpers + stagnation/research-context helpers.

Extracted from TaskLoop: these four helpers are called from TaskLoop.run()'s
main loop but don't participate in that loop's own control-flow state
(all_responses, all_files, task_num, ...) — they take everything they need
as explicit parameters and return results rather than mutating shared
locals, so they're a clean seam.

check_stagnation and build_research_extra were already @staticmethod with
no `self` usage at all, so they're plain functions here. run_criterion_loop
and run_acceptance_loop need TaskLoopDeps + a logger, so they're grouped in
a small _FixCycleRunner class (same composition pattern as
CriterionEvaluator/task_exec_ctx elsewhere in this package) rather than
free functions with 8+ parameters each.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import structlog

from agent.orchestration.task_exec_ctx import TaskLoopDeps, _TaskExecCtx

logger = structlog.get_logger()

_AUTO_PREFIXES = ("file exists:", "file contains:", "command exits 0:")


def check_stagnation(
    score: int,
    prev_score: int,
    plateau_count: int,
    zero_score_count: int,
    task_summaries: list[str],
) -> tuple[bool, int, int]:
    """Return (stop, plateau_count, zero_score_count)."""
    if score == 0:
        zero_score_count += 1
        if zero_score_count >= 2:
            task_summaries.append(f"🔍 **Verifier** stuck at 0/10 for {zero_score_count} rounds — stopping")
            return True, plateau_count, zero_score_count
    else:
        zero_score_count = 0

    if prev_score >= 0:
        drop = prev_score - score
        if drop >= 2:
            task_summaries.append(
                f"🔍 **Verifier** stagnated at {score}/10 (dropped {drop} pts from {prev_score}/10) — stopping"
            )
            return True, plateau_count, zero_score_count
        if score == prev_score and score >= 5:
            plateau_count += 1
            if plateau_count >= 2:
                task_summaries.append(f"🔍 **Verifier** stagnated at {score}/10 (plateau ×2) — stopping")
                return True, plateau_count, zero_score_count
        else:
            plateau_count = 0

    return False, plateau_count, zero_score_count


def build_research_extra(deps: TaskLoopDeps, agent_type: str, task_outputs: dict[str, list[str]]) -> str:
    research_outputs = task_outputs.get("research", []) + task_outputs.get("researcher", [])
    if not research_outputs:
        return ""
    snippet_cap = deps.context_builder.char_budget(fraction=0.015, cap=3_000)
    doc_cap = deps.context_builder.char_budget(fraction=0.08, cap=40_000)
    if agent_type in ("develop", "test"):
        snippets = [s[:snippet_cap] for s in research_outputs[-2:]]
        return "\n\n## Prior research findings\n\n" + "\n\n---\n\n".join(snippets)
    if agent_type == "documenter":
        return "\n\n## Research findings to synthesize\n\n" + "\n\n---\n\n".join(
            s[:doc_cap] for s in research_outputs
        )
    return ""


class _FixCycleRunner:
    """Criterion-driven and acceptance-test fix-cycle iterations for TaskLoop."""

    def __init__(self, deps: TaskLoopDeps) -> None:
        self._d = deps
        self.logger = logger.bind(component="task_loop_cycles")

    async def run_criterion_loop(
        self,
        *,
        ctx: _TaskExecCtx,
        auto_failing: list,
        behavioral_failing: list,
        passed_count: int,
        criterion_fix_count: int,
        criterion_attempts: dict[str, int],
        fix_budget: int,
        objective: str,
        task_summaries: list[str],
        last_screenshot: Optional[str],
        run_final_verifier: Callable,
        completion_criteria: list[str],
    ) -> Optional[bool]:
        """Handle one criterion-loop iteration.

        Returns:
          None  — a fix task was injected; caller should increment counter and continue
          True  — all criteria resolved (passed or abandoned); caller should proceed to acceptance
        """
        d = self._d

        if not auto_failing:
            vr = await run_final_verifier()
            behavioral_note = (
                f" ({len(behavioral_failing)} behavioral criteria not auto-verifiable)"
                if behavioral_failing else ""
            )
            # Criteria passing is necessary but not sufficient — the mark
            # reflects the verifier's actual pass/fail, not the criteria
            # check alone, so a low score never renders as a checkmark.
            mark = "✅" if vr.passed else "⚠️"
            task_summaries.append(
                f"{mark} **All auto-checkable criteria satisfied**{behavioral_note} — score {vr.score}/10"
            )
            for c in criterion_attempts:
                d.criterion_score_store.record(c, succeeded=True)
            return True

        if criterion_fix_count >= fix_budget:
            vr = await run_final_verifier()
            task_summaries.append(
                f"🎯 Fix budget ({fix_budget}) exhausted — {passed_count}/{len(completion_criteria)} criteria passing — score {vr.score}/10"
            )
            still_failing = {r.criterion for r in auto_failing}
            for c in criterion_attempts:
                d.criterion_score_store.record(c, succeeded=c not in still_failing)
            return True

        target = next(
            (r for r in auto_failing
             if criterion_attempts.get(r.criterion, 0) < d.criterion_score_store.attempt_budget(r.criterion)),
            None,
        )
        if target is None:
            vr = await run_final_verifier()
            task_summaries.append(f"🔍 All auto-checkable failing criteria abandoned — score {vr.score}/10")
            still_failing = {r.criterion for r in auto_failing}
            for c in criterion_attempts:
                d.criterion_score_store.record(c, succeeded=c not in still_failing)
            return True

        criterion_attempts[target.criterion] = criterion_attempts.get(target.criterion, 0) + 1
        fix_spec = d.verifier_coordinator.make_targeted_fix_spec(
            target, objective, criterion_fix_count + 1, screenshot_path=last_screenshot
        )
        ctx.add_task(fix_spec["description"], fix_spec["agent_type"])
        task_summaries.append(
            f"🎯 **Criterion fix** ({criterion_fix_count + 1}/{fix_budget}) — failing: {target.criterion[:60]}"
        )
        self.logger.info(
            "criterion_fix_injected",
            criterion=target.criterion[:60],
            attempt=criterion_attempts[target.criterion],
            fix_num=criterion_fix_count + 1,
        )
        return None  # fix injected — caller increments and continues

    async def run_acceptance_loop(
        self,
        *,
        ctx: _TaskExecCtx,
        acceptance_criteria: list[str],
        acceptance_fix_count: int,
        acceptance_budget: int,
        acc_criterion_attempts: dict[str, int],
        objective: str,
        task_summaries: list[str],
        last_screenshot: Optional[str],
        screenshot_path: Optional[str],
        shell_fn: Callable,
        combined_response: str = "",
    ) -> tuple[bool, int, Optional[str], Optional[str]]:
        """Run one acceptance-test iteration.

        Returns (done, acceptance_fix_count, last_screenshot, screenshot_path).
        done=True means break; done=False means continue (fix injected).
        """
        from agent.orchestration.app_probe import AppProbe

        d = self._d
        ws_acc = Path(getattr(d.tool_executor, "workspace_path", ".") if d.tool_executor else ".")

        # Auto-checkable criteria must be evaluated via filesystem/shell — not via
        # LLM/screenshot.  Planners sometimes misplace these in acceptance_criteria;
        # evaluating them here prevents spurious "file not found" failures that each
        # trigger a fix task and corrupt the workspace.
        auto_crit = [c for c in acceptance_criteria if any(c.lower().startswith(p) for p in _AUTO_PREFIXES)]
        visual_crit = [c for c in acceptance_criteria if c not in set(auto_crit)]

        auto_results: list = []
        if auto_crit:
            auto_results = await d.verifier_coordinator.evaluate_criteria(auto_crit, ws_acc, shell_fn)

        visual_results: list = []
        if visual_crit:
            app_probe = AppProbe(ws_acc, shell_fn=shell_fn)
            visual_results = await d.verifier_coordinator.run_acceptance_tests(
                visual_crit, ws_acc, app_probe, d.acceptance_tester_agent,
                agent_output=combined_response,
            )
            cap = d.verifier_coordinator.last_screenshot_path
            if cap:
                last_screenshot = cap
                screenshot_path = cap

        acc_results = auto_results + visual_results

        if not acc_results:
            task_summaries.append("⏭️ Acceptance tests skipped — no server entry point detected")
            return True, acceptance_fix_count, last_screenshot, screenshot_path

        acc_failing = [r for r in acc_results if not r.passed]
        acc_passed = len(acc_results) - len(acc_failing)

        if not acc_failing:
            task_summaries.append(f"✅ **All {len(acceptance_criteria)} acceptance criteria satisfied**")
            return True, acceptance_fix_count, last_screenshot, screenshot_path

        if acceptance_fix_count >= acceptance_budget - 1:
            # Record still-failing criteria so the store learns from this session.
            for r in acc_failing:
                d.criterion_score_store.record(r.criterion, succeeded=False)
            task_summaries.append(
                f"🎯 Acceptance budget ({acceptance_budget}) exhausted — {acc_passed}/{len(acceptance_criteria)} passing"
            )
            return True, acceptance_fix_count, last_screenshot, screenshot_path

        # Filter out criteria that have exhausted their per-criterion budget.
        # This prevents the acceptance loop from retrying a consistently-failing
        # criterion (e.g. cargo check) more times than confidence warrants.
        actionable_failing = [
            r for r in acc_failing
            if acc_criterion_attempts.get(r.criterion, 0) < d.criterion_score_store.attempt_budget(r.criterion)
        ]
        if not actionable_failing:
            # Record abandoned criteria as failures so the store learns.
            for r in acc_failing:
                d.criterion_score_store.record(r.criterion, succeeded=False)
            task_summaries.append(
                f"🎯 All failing acceptance criteria exhausted their per-criterion budget — "
                f"{acc_passed}/{len(acceptance_criteria)} passing"
            )
            return True, acceptance_fix_count, last_screenshot, screenshot_path

        acceptance_fix_count += 1
        acc_target = actionable_failing[0]
        acc_criterion_attempts[acc_target.criterion] = acc_criterion_attempts.get(acc_target.criterion, 0) + 1
        acc_fix_spec = d.verifier_coordinator.make_targeted_fix_spec(
            type("CR", (), {"criterion": acc_target.criterion, "passed": False, "detail": acc_target.detail})(),
            objective,
            acceptance_fix_count,
            screenshot_path=last_screenshot,
        )
        ctx.add_task(acc_fix_spec["description"], acc_fix_spec["agent_type"])
        task_summaries.append(
            f"🖼️ **Acceptance fix** ({acceptance_fix_count}/{acceptance_budget}) — {acc_target.criterion[:60]}"
        )
        self.logger.info(
            "acceptance_fix_injected",
            criterion=acc_target.criterion[:60],
            fix_num=acceptance_fix_count,
            attempt=acc_criterion_attempts[acc_target.criterion],
        )
        return False, acceptance_fix_count, last_screenshot, screenshot_path
