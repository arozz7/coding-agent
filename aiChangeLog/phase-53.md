# Phase 53 — Structural debt: research_agent.py (Phase C task 5/6)

Source: `docs/plans/codebase-improvement-plan.md`, Phase C.

## Changes

- **New `agent/agents/research_routing.py`** — the module-level routing
  logic moved out: `_SEARCH_TRIGGERS`, `_LOCAL_TASK_RE`, `_FILE_WRITE_RE`
  regexes, `_needs_web_search`, `_is_pdf_url`, `_emit`, `_DOCUMENT_EXTS`,
  `_PDF_MAGIC`. All pure functions/constants with zero coupling to
  `ResearchRole`'s instance state (`self.logger`, `self.get_system_prompt()`).
  `research_agent.py` imports them back; `_needs_web_search` remains
  importable as `agent.agents.research_agent._needs_web_search` (re-export
  via the import), matching the existing
  `from agent.agents.research_agent import _needs_web_search` in
  `tests/unit/test_research_local_dirs.py` — no test changes needed.

## Scope note (smaller than the original plan estimate)

The plan's Phase C write-up suggested "separate query decomposition/gap-fill
passes from synthesis & file output" as the split boundary. Reading the file
in full (done before starting any Phase C work) showed the bulk of the
remaining code — `_decompose`, `_identify_gaps`, `_search_question`,
`_check_coverage`, `_synthesize`, `_write_research_files`,
`_save_raw_research`, `_extract_document_paths`, `_extract_mentioned_files`,
`_scan_task_dirs`, ~365 lines — are all `ResearchRole` instance methods
using `self.logger`/`self.name`/`self.get_system_prompt()`, not separable
pure functions the way the routing logic was. Splitting those further would
mean converting them to free functions or a delegate class carrying its own
logger, for a file that already clears the 600-line hard limit after the
routing extraction alone. Stopped here rather than force a riskier split for
marginal gain — same judgment call as `model_router.py` in Phase 50.

## Verification

- `python -m pytest tests -q` → 567 passed, zero test changes needed.
- `ruff check agent api llm mcp observability` → 0 errors.
- `wc -l`: `research_agent.py` 703 → 591 lines (now under the 600 hard
  limit). New `research_routing.py`: 130 lines.

## Remaining Phase C tasks

6. `agent/agents/developer_agent.py` → `output_blocks.py` + `fix_loop.py` (highest risk; needs a characterization-test check before touching the fix loop)
