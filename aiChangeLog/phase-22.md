# Phase 22: Agent Loop Supervisor & UI Resilience Fixes

## Overview
Following the Phase 21 system prompt refactoring, we identified and resolved an elusive job stability bug during long-running tasks. The supervisor's `_check_stale_job` watchdog was prematurely terminating the API when jobs appeared stuck without activity (usually on the 4th/5th step). This was traced to the `DeveloperAgent` hitting a timeout while executing infinite daemon processes (e.g. `npm start`) and then entering an unbound fix loop without emitting progress heartbeats back to the supervisor via `updated_at`.

## Files Modified

**Agent / Orchestrator Layer**
*   `agent/agents/developer_agent.py`: 
    - Extracted `on_phase` from context directly within the DeveloperAgent's iterative loop, calling it sequentially for `fixing:attempt:1...N` to emit progress heartbeats.
    - Added early-abort (`made_progress = len(iteration_files) > 0`) in the developer loop to cleanly `break` if the tool fails to produce valid file edits. This directly prevents hitting the 50 iteration timeout (`MAX_FIX_ITERATIONS`) on uncorrectable daemon errors.
*   `agent/orchestrator.py`: 
    - Introduced a `_wrapped_on_phase` hook in `_orchestrate_task` which cleanly concatenates the overarching task step descriptor (e.g. `task:4/5`) with the inner tool attempts, resulting in fully traceable states like `task:4/5:fixing:attempt:N`.

## Behavior Changes
- Job iterations safely report sub-progress to the Orchestrator, correctly resetting the `_STALE_JOB_THRESHOLD_SECS` failsafe timer across deep developer action loops.
- The Developer loop avoids 60s x 50 iterations locked dead-time when the AI is simply not making direct file changes, instead cleanly short-circuiting back to the main processing tree.
- Discord/UI progress trackers show transparent internal loops linked perfectly to their overarching plan phases without breaking the expected naming map schema.

## Conclusion
The loop logic is heavily fortified against false-positives timeouts from infinite-running sub-processes, preventing unexpected backend kills during daemonized tasks.
