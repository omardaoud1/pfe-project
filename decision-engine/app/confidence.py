from typing import List, Dict, Any, Tuple

# How many recent records to consider
LOOKBACK = 5

# Per-entry adjustments
SUCCESS_BONUS = 0.05   # × 5 successes = +0.10
FAILURE_PENALTY = 0.7 # × 2 failures  = -0.30


def compute_confidence(
    base_confidence: float,
    history: List[Dict[str, Any]],
) -> Tuple[float, bool]:
    """
    Compute final confidence from the last LOOKBACK (5) history entries.

    Rules (applied to the last 5 records for this incident_key):
      • Each success  → +0.02   (5 successes = +0.10)
      • Each failure  → -0.15   (2 failures  = -0.30)
      Both adjustments are cumulative and can apply together.

    Returns:
        (final_confidence, history_used)
        history_used = False when history is empty (base values used as-is)
    """
    if not history:
        return round(base_confidence, 2), False

    # Take only the most recent LOOKBACK entries
    recent = history[-LOOKBACK:]

    success_count = sum(1 for e in recent if e.get("success", False))
    failure_count = sum(1 for e in recent if not e.get("success", True))

    adjustment = (success_count * SUCCESS_BONUS) - (failure_count * FAILURE_PENALTY)
    final_confidence = base_confidence + adjustment

    # Clamp to [0.0, 1.0]
    final_confidence = max(0.0, min(1.0, final_confidence))

    return round(final_confidence, 2), True
