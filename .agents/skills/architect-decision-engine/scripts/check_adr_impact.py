import sys
import json
from typing import List, Dict, Any, Union

# High-impact keywords that require architectural oversight
IMPACT_KEYWORDS: List[str] = ["database", "schema", "auth", "api", "migration", "refactor", "security"]

def analyze_prompt(prompt: str) -> Dict[str, Any]:
    """
    Analyzes the user prompt for high-impact keywords that might necessitate an ADR.

    Args:
        prompt: The user's input prompt.

    Returns:
        Dict[str, Any]: Assessment result containing boolean flag, impacted areas, and a message.
    """
    found: List[str] = [word for word in IMPACT_KEYWORDS if word in prompt.lower()]
    return {
        "requires_adr": len(found) > 0,
        "impact_areas": found,
        "message": f"Architectural impact detected in: {', '.join(found)}" if found else "Low architectural impact."
    }

if __name__ == "__main__":
    user_input = sys.argv[1] if len(sys.argv) > 1 else ""
    print(json.dumps(analyze_prompt(user_input)))