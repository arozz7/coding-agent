"""Research-task routing — local-vs-web decision, PDF/file-write detection.

Extracted from research_agent.py: these are pure module-level functions and
regexes with zero coupling to ResearchRole's instance state (self.logger,
self.get_system_prompt, ...), unlike the bulk of ResearchRole's methods
(_decompose, _synthesize, _write_research_files, ...).
"""
from __future__ import annotations

import re

_DOCUMENT_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".tsv"}

# PDF MIME signature — detect binary content that slipped through web_fetch.
_PDF_MAGIC = "%PDF"


def _is_pdf_url(url: str) -> bool:
    lower = url.lower()
    return lower.endswith(".pdf") or "/pdf/" in lower


# Patterns that always trigger the iterative web-research path.
_SEARCH_TRIGGERS = re.compile(
    r"\b("
    r"search\s+(for|the\s+web|online)|look\s+up|find\s+online|google|"
    r"what('s|\s+is)\s+the\s+(latest|current|news)|current\s+version|"
    r"recent\s+news|last\s+night|yesterday|today|latest|recent(ly)?|"
    r"score|scores|weather|stock|price|market|news|headline|"
    r"who\s+(won|lost|is)|what\s+happened|"
    r"released|launched|announced|"
    r"research\s+(on|about|into|for)|deep\s+research|in.?depth|"
    r"comprehensive|thorough|exhaustive|"
    r"investigate|explore|study|analyze|analyse|"
    r"build.*agent|how\s+to\s+build|"
    r"best\s+practices?|compare|evaluate|assess|"
    r"survey|overview|landscape|state\s+of"
    r")\b",
    re.IGNORECASE,
)

# Patterns that indicate the user wants output captured to markdown/files.
_FILE_WRITE_RE = re.compile(
    r"\b("
    r"capture\s+(to|into|in)\s+(markdown|files?|docs?|documents?)|"
    r"save\s+(to|into|as)\s+(markdown|files?|docs?)|"
    r"write\s+(to|into)\s+(markdown|files?|docs?)|"
    r"create\s+(markdown\s+files?|docs?|documents?|files?)|"
    r"output\s+(to|as)\s+(markdown|files?)|"
    r"logically\s+(into\s+)?(files?|docs?|markdown)|"
    r"into\s+markdown\s+files?|as\s+markdown\s+files?"
    r")\b",
    re.IGNORECASE,
)


# Patterns that indicate the task is about local workspace content — no web
# search needed even if no specific file names are mentioned.
#
# IMPORTANT: the routing default is web search; only LOCAL signals can suppress it.
# Each branch uses its own word-boundary markers because some patterns (src/, dist/)
# end on non-word characters and cannot use a trailing \b.
_LOCAL_TASK_RE = re.compile(
    r"("
    # Workspace / codebase explicit references
    r"\bin\s+the\s+(workspace|project|codebase|repo(?:sitory)?)\b|"
    r"\blast\s+(failed\s+)?(job|error|run|task|build)\b|"
    r"\b(?:find|show|check|look\s+at)\s+(?:the\s+)?(?:errors?|bugs?|issues?|logs?|output|files?)\b|"
    r"\bwhat\s+(?:is|was|went)\s+wrong\b|"
    r"\bwhy\s+(?:is|did|does)\s+it\s+fail\b|"
    # Phase / documentation / plan references (with or without space/underscore — covers filenames like PHASE1.md)
    r"\bphase[\s_\-]?\d+\b|"
    r"\b(the|our|current|existing)\s+(docs?|documentation|plans?|impl(?:ementation)?\s+plan|change.?log)\b|"
    r"\b(across|in|from)\s+(the\s+)?(docs?|documents?|plans?|files?)\b|"
    r"\baiChangeLog\b|\bagent.wiki\b|\bimplementation\s+plan\b|"
    # Any filename with a recognisable extension — these are always local workspace files
    r"\b[\w][\w\-\.]*\.(json|yaml|yml|toml|md|ts|tsx|js|jsx|py|rs|go|java|cs|css|scss|html?|env|lock|config|ini|cfg|sh|ps1|sql|txt|log)\b|"
    # Directory path fragments (no trailing \b — / is not a word char)
    r"\bsrc/|\bdist/|\blib/|\bpublic/|\bassets/|\btests?/|\bnode_modules/|"
    # Git operations — always about the local repo
    r"\bgit\s+(status|log|diff|branch|commit|push|pull|blame|show|stash|merge|rebase)\b|"
    # Package manager operations — always local project context
    r"\bnpm\s+(run|install|build|test|start|ci|update|uninstall)\b|"
    r"\byarn\s+(run|install|build|test|start|add|remove)\b|"
    r"\bpnpm\s+(run|install|build|test|start|add|remove)\b|"
    r"\bcargo\s+(build|run|test|check|clippy|fmt)\b|"
    # Structure / layout requests — always local
    r"\b(directory|file|project)\s+structure\b|"
    # Diagnostic / output analysis tasks — always about local workspace state
    r"\bterminal\s+(output|log)\b|"
    r"\bcompilation\s+(error|failure|output|log)\b|"
    r"\bbuild\s+(error|failure|output|log)\b|"
    r"\berror\s+log\b|"
    r"\broot\s+cause\b|"
    r"\bstep\s+\d+\b|"
    r"\bread\s+(any\s+)?(source\s+)?(files?|logs?|output)\b|"
    r"\bfailed\s+to\s+(build|compile|run|start|launch)\b|"
    r"\banalyze\b.*\b(output|log|error|file|build|crash|failure)\b|"
    r"\b(output|log|error|file|build|crash|failure)\b.*\banalyze\b"
    r")",
    re.IGNORECASE,
)


def _needs_web_search(task: str, local_sections: list[str]) -> bool:
    """Decide whether *task* requires a live web search.

    Default to web search for research tasks. Only skip it when the task
    explicitly refers to the local workspace/codebase (errors, logs, files).
    _SEARCH_TRIGGERS was too narrow — planner-generated subtasks like
    "Research state persistence..." don't contain trigger words but clearly
    need web search, not local file scanning.

    Exception: if we successfully read local directory content AND the task
    has no explicit web-search signals, skip web — it would return generic
    noise. Tasks that want both ("review docs AND search for gaps") still
    get web search — an explicit web signal always wins.
    """
    found_local_dirs = any(s.startswith("Contents of ") for s in local_sections)
    has_web_signals = bool(_SEARCH_TRIGGERS.search(task))
    is_local_task = bool(_LOCAL_TASK_RE.search(task)) or (found_local_dirs and not has_web_signals)
    return has_web_signals or not is_local_task


def _emit(on_phase, label: str) -> None:
    if on_phase:
        try:
            on_phase(label)
        except Exception:
            pass
