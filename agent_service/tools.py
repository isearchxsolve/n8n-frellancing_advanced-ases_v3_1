"""
ASES - Utility Functions
"""

from typing import Dict, Any


def calculate_cost(tokens: int, model: str) -> float:
    """
    Calculate approximate cost in USD.
    Assumes 2:1 output:input ratio for simplicity.
    """
    pricing = {
        "gpt-4o": {"input": 0.0025, "output": 0.0100},
        "gpt-4o-mini": {"input": 0.00015, "output": 0.00060},
        "claude-3-5-sonnet": {"input": 0.0030, "output": 0.0150}
    }

    p = pricing.get(model, pricing["gpt-4o-mini"])
    # Rough estimate: 1/3 input, 2/3 output
    input_cost = (tokens / 3) / 1000 * p["input"]
    output_cost = (tokens * 2 / 3) / 1000 * p["output"]

    return round(input_cost + output_cost, 4)


def truncate_context(context: str, max_chars: int = 8000) -> str:
    """
    Truncate context to fit within token limits while preserving structure.
    """
    if len(context) <= max_chars:
        return context

    # Keep the beginning (instructions) and end (recent errors), truncate middle
    head = context[:max_chars // 3]
    tail = context[-max_chars // 3:]

    return head + "\n\n... [truncated] ...\n\n" + tail
