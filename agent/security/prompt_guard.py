"""Prompt injection detection and input sanitization for user-supplied task strings."""

import re
import unicodedata

import structlog

logger = structlog.get_logger()

# Phrases that indicate an attempt to override the system prompt.
# These are partial-match, case-insensitive patterns.
_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|directives?)",
        r"disregard\s+(all\s+)?(previous|prior|above|earlier)",
        r"forget\s+(all\s+)?(previous|prior|above|earlier|your)\s+(instructions?|prompts?|training)",
        r"you\s+are\s+now\s+a\s+different",
        r"new\s+instructions?\s*:",
        r"override\s+(your\s+)?(previous\s+)?(instructions?|programming|guidelines?)",
        r"do\s+not\s+follow\s+(your\s+)?(previous\s+|prior\s+)?(instructions?|guidelines?)",
        r"act\s+as\s+a\s+different\s+(ai|agent|assistant|model)",
        r"system\s*:\s*(you\s+are|ignore\s+all)",
        r"<\s*/?system\s*>",                # XML-style system-prompt injection
        r"\[INST\]\s*ignore",               # Llama instruction injection marker
        r"###\s*new\s+instruction",         # Markdown instruction-override header
    ]
]

# Upper bound on accepted task length (chars). Prevents token-stuffing attacks.
_MAX_INPUT_LENGTH = 32_000


def sanitize_user_input(text: str) -> str:
    """Strip dangerous control characters and enforce a length cap.

    Removes null bytes, low ASCII control characters (except \\t \\n \\r),
    and Unicode private-use code points that have no place in a task string.
    Does NOT reject or alter legitimate developer instructions.
    """
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    cleaned = "".join(c for c in cleaned if unicodedata.category(c) != "Co")
    return cleaned[:_MAX_INPUT_LENGTH]


def detect_injection(text: str) -> bool:
    """Return True if *text* contains known prompt-injection markers.

    Callers decide whether to block or log — this function only detects.
    """
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return True
    return False


def guard_task(task: str) -> str:
    """Sanitize *task* and raise ValueError if injection is detected.

    Returns the sanitized task string on success.

    Raises:
        ValueError: If a prompt-injection pattern is found.
    """
    clean = sanitize_user_input(task)
    if detect_injection(clean):
        logger.warning("prompt_injection_detected", task_preview=clean[:120])
        raise ValueError(
            "Task rejected: possible prompt injection detected. "
            "Please rephrase your request without instruction-override phrases."
        )
    return clean
