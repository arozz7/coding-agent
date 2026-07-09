"""Session/job ID generation.

A bare `session_<timestamp>` (second-level precision) collides whenever two
tasks arrive in the same second — plausible with Discord + API + subagents
all submitting concurrently. Appending a short random suffix keeps IDs both
human-scannable (still timestamp-sorted) and collision-safe.
"""

from datetime import datetime, timezone
from uuid import uuid4


def new_session_id(prefix: str = "session") -> str:
    """Return a `<prefix>_<UTC timestamp>_<6 hex chars>` ID."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{uuid4().hex[:6]}"
