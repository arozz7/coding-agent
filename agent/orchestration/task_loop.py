"""task_loop — core planning + execution + verification loop.

Extracted from AgentOrchestrator._run_task_loop.  The two previously
duplicated verification paths (job_id vs. no-persistence) have been unified
via _TaskExecCtx (agent/orchestration/task_exec_ctx.py), a small abstraction
that wraps the four operations that differ between the two modes:
  - fetch_next()    — next pending task spec
  - add_task()      — append a dynamically-generated fix/new task
  - mark_running()  — persist "running" status
  - mark_done()     — persist final status + summary

All orchestration logic is identical to the original; only the branching on
`if job_id` has been replaced by ctx.method() calls.

The criterion/acceptance fix-cycle iterations and the stagnation/research-
context helpers live in task_loop_cycles.py — they're called from run()'s
main loop but don't participate in its control-flow state directly.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import structlog

from agent.orchestration.task_exec_ctx import TaskLoopDeps, _TaskExecCtx
from agent.orchestration.task_loop_cycles import _FixCycleRunner, build_research_extra, check_stagnation

if TYPE_CHECKING:
    from agent.agents.verifier_agent import VerifierResult

logger = structlog.get_logger()

_VERIFIABLE_TYPES = {"develop", "research", "sdlc"}
_AUTO_PREFIXES = ("file exists:", "file contains:", "command exits 0:")


# ---------------------------------------------------------------------------
# TaskLoop
# ---------------------------------------------------------------------------

class TaskLoop:
    """Executes the plan → task loop → criterion fixes → verifier cycle."""

    def __init__(self, deps: TaskLoopDeps) -> None:
        self._d = deps
        self._fix_cycles = _FixCycleRunner(deps)
        self.logger = logger.bind(component="task_loop")

    async def run(
        self,
        objective: str,
        task_type: str,
        session_id: str,
        on_phase: Optional[Callable[[str], None]] = None,
        job_id: Optional[str] = None,
    ) -> dict:
        d = self._d

        def _emit(label: str) -> None:
            if on_phase:
                try:
                    on_phase(label)
                except Exception:
                    pass

        # 1. Plan
        _emit("planning:tasks")
        _ws_path = Path(getattr(d.tool_executor, "workspace_path", ".") if d.tool_executor else ".")
        planning_ctx = await d.context_builder.build_planning_context(objective)
        plan_result = await d.planner_agent.plan_with_criteria(
            objective,
            context=planning_ctx,
            task_type=task_type,
            workspace=_ws_path,
        )
        task_specs = list(plan_result.tasks)
        completion_criteria = plan_result.completion_criteria
        acceptance_criteria = plan_result.acceptance_criteria

        # 1b. Plan review
        if task_type in ("develop", "sdlc") and len(task_specs) >= 3:
            _emit("planning:review")
            task_specs = await d.plan_reviewer_agent.review(task_specs, objective)

        # 2. Persist tasks
        ctx = _TaskExecCtx(job_id, task_specs, d.task_store)
        ctx.persist_tasks(task_specs)

        self.logger.info(
            "task_loop_started",
            objective=objective[:80],
            task_count=ctx.total,
            job_id=job_id,
        )

        # 3. Loop state
        all_responses: list[str] = []
        all_files: list[str] = []
        task_summaries: list[str] = []
        screenshot_path: Optional[str] = None
        task_num = 0
        _task_outputs: dict[str, list[str]] = {}
        _verifier_rounds = 0
        _prev_verifier_score = -1
        _plateau_count = 0
        _zero_score_count = 0
        _final_verifier_score: int | None = None
        _verifier_snapshot_files: set[str] = set()
        _fix_budget = max(1, int(os.getenv("FIX_BUDGET", "20")))
        _criterion_fix_count = 0
        _criterion_attempts: dict[str, int] = {}
        _acceptance_budget = max(1, int(os.getenv("ACCEPTANCE_BUDGET", "5")))
        _acceptance_fix_count = 0
        _acc_criterion_attempts: dict[str, int] = {}
        _last_acceptance_screenshot: Optional[str] = None

        if task_type in ("develop", "sdlc"):
            _MAX_VERIFIER_ROUNDS = max(1, int(os.getenv("DEVELOP_VERIFIER_ROUNDS", "6")))
        else:
            _MAX_VERIFIER_ROUNDS = max(1, int(os.getenv("RESEARCH_VERIFIER_ROUNDS", "3")))

        # ------------------------------------------------------------------
        # Helpers — defined here to close over loop state
        # ------------------------------------------------------------------

        async def _shell(cmd: str) -> str:
            if not d.tool_executor:
                return ""
            try:
                out = await d.tool_executor.execute("shell", {"command": cmd})
                return str(out) if out else ""
            except Exception:
                return ""

        async def _run_final_verifier() -> "VerifierResult":
            nonlocal _final_verifier_score
            _emit("verifying:final")
            combined = "\n\n---\n\n".join(all_responses)
            vr = await d.verifier_coordinator.run_verification(
                objective, task_type, combined, all_files, tool_executor=d.tool_executor
            )
            _final_verifier_score = vr.score
            if vr.passed:
                try:
                    d.session_memory.store_episodic(session_id, objective, combined[:500], vr.score, task_type)
                except Exception:
                    pass
            return vr

        # ------------------------------------------------------------------
        # 4. Main loop
        # ------------------------------------------------------------------
        while True:
            task_obj = ctx.fetch_next()

            # ---- All tasks done — run verification ----
            if task_obj is None:
                # Criterion-driven fix loop
                if completion_criteria and task_type in _VERIFIABLE_TYPES:
                    ws_crit = Path(getattr(d.tool_executor, "workspace_path", ".") if d.tool_executor else ".")
                    combined_so_far = "\n\n---\n\n".join(all_responses)
                    _emit(f"verifying:criteria-{_criterion_fix_count + 1}")
                    crit_results = await d.verifier_coordinator.evaluate_criteria(
                        completion_criteria, ws_crit, _shell, combined_so_far
                    )
                    _failing = [r for r in crit_results if not r.passed]
                    _passed_count = len(crit_results) - len(_failing)

                    _auto_failing = [
                        r for r in _failing
                        if any(r.criterion.lower().startswith(p) for p in _AUTO_PREFIXES)
                    ]
                    _behavioral_failing = [r for r in _failing if r not in _auto_failing]

                    criteria_done = await self._fix_cycles.run_criterion_loop(
                        ctx=ctx,
                        auto_failing=_auto_failing,
                        behavioral_failing=_behavioral_failing,
                        passed_count=_passed_count,
                        criterion_fix_count=_criterion_fix_count,
                        criterion_attempts=_criterion_attempts,
                        fix_budget=_fix_budget,
                        objective=objective,
                        task_summaries=task_summaries,
                        last_screenshot=_last_acceptance_screenshot,
                        run_final_verifier=_run_final_verifier,
                        completion_criteria=completion_criteria,
                    )
                    if criteria_done is None:
                        # fix task was injected — keep looping
                        _criterion_fix_count += 1
                        continue
                    # criteria_done = True means all criteria resolved
                    if not criteria_done:
                        break  # shouldn't happen

                    # Acceptance test loop
                    if acceptance_criteria and task_type in ("develop", "sdlc") and _acceptance_fix_count < _acceptance_budget:
                        acc_done, _acceptance_fix_count, _last_acceptance_screenshot, screenshot_path = \
                            await self._fix_cycles.run_acceptance_loop(
                                ctx=ctx,
                                acceptance_criteria=acceptance_criteria,
                                acceptance_fix_count=_acceptance_fix_count,
                                acceptance_budget=_acceptance_budget,
                                acc_criterion_attempts=_acc_criterion_attempts,
                                objective=objective,
                                task_summaries=task_summaries,
                                last_screenshot=_last_acceptance_screenshot,
                                screenshot_path=screenshot_path,
                                shell_fn=_shell,
                                combined_response="\n\n---\n\n".join(all_responses),
                            )
                        if not acc_done:
                            continue
                    break

                # Holistic verifier loop (no completion_criteria)
                elif task_type in _VERIFIABLE_TYPES and _verifier_rounds < _MAX_VERIFIER_ROUNDS:
                    combined_so_far = "\n\n---\n\n".join(all_responses)
                    _emit(f"verifying:round-{_verifier_rounds + 1}")
                    _new_files = [f for f in all_files if f not in _verifier_snapshot_files]
                    _verifier_snapshot_files = set(all_files)
                    vresult = await d.verifier_coordinator.run_verification(
                        objective, task_type, combined_so_far, all_files,
                        tool_executor=d.tool_executor,
                    )
                    self.logger.info(
                        "verifier_result",
                        round=_verifier_rounds + 1,
                        score=vresult.score,
                        passed=vresult.passed,
                        gaps=len(vresult.gaps),
                    )
                    _verifier_rounds += 1
                    _final_verifier_score = vresult.score
                    if vresult.passed:
                        try:
                            d.session_memory.store_episodic(
                                session_id, objective, combined_so_far[:500], vresult.score, task_type,
                            )
                        except Exception:
                            pass
                    if not vresult.passed:
                        stop, _plateau_count, _zero_score_count = check_stagnation(
                            vresult.score, _prev_verifier_score, _plateau_count, _zero_score_count, task_summaries
                        )
                        if stop:
                            break
                        fix_specs = d.verifier_coordinator.make_fix_specs(
                            objective, task_type, vresult, _verifier_rounds,
                            files_created=all_files,
                            files_changed_this_round=_new_files,
                            prev_score=_prev_verifier_score,
                        )
                        _prev_verifier_score = vresult.score
                        ctx.add_tasks(fix_specs)
                        task_summaries.append(
                            f"🔍 **Verifier** (round {_verifier_rounds}/{_MAX_VERIFIER_ROUNDS}) "
                            f"— score {vresult.score}/10, injecting {len(fix_specs)} fix task(s)"
                        )
                        continue
                break

            # ---- Execute task ----
            task_id = task_obj["task_id"]
            task_num = task_obj["sequence"]
            description = task_obj["description"]
            agent_type = task_obj["agent_type"]
            ctx.total = max(ctx.total, task_num)
            ctx.mark_running(task_id)

            _emit(f"task:{task_num}/{ctx.total}:{agent_type}:{description[:40]}")
            self.logger.info(
                "task_loop_executing",
                task_num=task_num,
                total=ctx.total,
                agent_type=agent_type,
                description=description[:60],
            )

            _current_agent_type = agent_type

            def _wrapped_phase(inner_label: str, _at=_current_agent_type) -> None:
                _emit(f"task:{task_num}/{ctx.total}:{_at}:{inner_label}")

            try:
                _extra = build_research_extra(d, agent_type, _task_outputs)
                result = await d.run_agent_fn(
                    description,
                    agent_type,
                    session_id,
                    on_phase=_wrapped_phase,
                    job_id=None,
                    _direct=True,
                    extra_context=_extra,
                )

                switch_notices = d.drain_switch_fn(on_phase)
                for notice in switch_notices:
                    all_responses.append(notice)
                    task_summaries.append(notice)

                if result.get("success"):
                    response_text = result.get("response", "")
                    all_responses.append(f"**Task {task_num}: {description[:60]}**\n\n{response_text}")
                    new_files = result.get("files_created", [])
                    all_files.extend(new_files)
                    if result.get("screenshot_path"):
                        screenshot_path = result["screenshot_path"]

                    _store_cap = d.context_builder.char_budget(fraction=0.05, cap=10_000)
                    _task_outputs.setdefault(agent_type, []).append(response_text[:_store_cap])

                    if agent_type in ("develop", "developer") and new_files:
                        try:
                            d.memory_wiki.update_from_files(new_files)
                        except Exception:
                            pass

                    new_task_specs = result.get("new_tasks", [])
                    if new_task_specs:
                        ctx.add_tasks(new_task_specs)
                        self.logger.info("new_tasks_added", count=len(new_task_specs), total=ctx.total)

                    ctx.mark_done(task_id, "done", response_text[:300])
                    completion_summary = result.get("completion_summary", "").strip()
                    short = completion_summary or response_text[:80].replace("\n", " ").strip()
                    task_summaries.append(f"✅ **{description[:60]}** — {short}")

                    if agent_type not in ("research", "researcher"):
                        try:
                            await d.skill_executor.execute_post(
                                "wiki-compile", description, result, d.context_builder.model_router
                            )
                        except Exception as we:
                            self.logger.warning("subtask_wiki_compile_failed", task_num=task_num, error=str(we))
                else:
                    error = result.get("error", "agent failed")
                    all_responses.append(f"**Task {task_num}: {description[:60]}** — failed: {error}")
                    task_summaries.append(f"❌ **{description[:60]}** — {error[:80]}")
                    ctx.mark_done(task_id, "failed", error)
                    self.logger.warning("task_loop_task_failed", task_num=task_num, error=error)

            except Exception as exc:
                self.logger.error("task_loop_exception", task_num=task_num, error=str(exc))
                ctx.mark_done(task_id, "failed", str(exc))
                all_responses.append(f"**Task {task_num}: {description[:60]}** — error: {exc}")
                task_summaries.append(f"❌ **{description[:60]}** — {str(exc)[:80]}")

            # Safety guard for no-persistence mode
            if not job_id and task_num >= len(task_specs):
                break

        combined = "\n\n---\n\n".join(all_responses) if all_responses else "(no output)"

        failed_count = sum(1 for s in task_summaries if s.startswith("❌"))
        done_count = sum(1 for s in task_summaries if s.startswith("✅"))
        if task_summaries:
            header = f"**{done_count}/{task_num} tasks completed**" + (f" · {failed_count} failed" if failed_count else "")
            next_steps = (
                "\n\n**Next steps:** Review the errors above. Use `!dev <description>` to continue."
                if failed_count
                else "\n\n**Next steps:** Changes applied. Run your test suite to verify, or `!result` to review."
            )
            job_summary = header + "\n\n" + "\n".join(task_summaries) + next_steps
        else:
            job_summary = ""

        seen: set[str] = set()
        unique_files = [f for f in all_files if not (f in seen or seen.add(f))]  # type: ignore[func-returns-value]

        self.logger.info("task_loop_complete", tasks_run=task_num, files_created=len(unique_files))
        return {
            "success": True,
            "response": combined,
            "files_created": unique_files,
            "screenshot_path": screenshot_path,
            "task_count": ctx.total,
            "job_summary": job_summary,
            "verifier_score": _final_verifier_score,
        }

