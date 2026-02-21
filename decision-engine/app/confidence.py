from typing import List, Dict, Any


def compute_confidence(
    base_confidence: float,
    history: List[Dict[str, Any]],
    max_adjustment: float = 0.3
) -> float:
    """
    Compute final confidence score.

    - base_confidence: value from static rules
    - history: past decisions for the same incident_key
    - max_adjustment: maximum total adjustment allowed
    """

    if not history:
        return round(base_confidence, 2)

    success_count = sum(1 for h in history if h["success"])
    failure_count = sum(1 for h in history if not h["success"])
    total = success_count + failure_count

    if total == 0:
        return round(base_confidence, 2)

    success_ratio = success_count / total

    # Adjustment logic
    adjustment = (success_ratio - 0.5) * 2 * max_adjustment
    final_confidence = base_confidence + adjustment

    # Clamp between 0 and 1
    final_confidence = max(0.0, min(1.0, final_confidence))

    return round(final_confidence, 2)

