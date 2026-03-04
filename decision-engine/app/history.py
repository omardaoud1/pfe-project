import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

HISTORY_FILE = Path("/app/decision_history.json")


def _load_history() -> List[Dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE, "r") as f:
        return json.load(f)


def _save_history(history: List[Dict[str, Any]]) -> None:
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, default=str)


def get_history(incident_key: str) -> List[Dict[str, Any]]:
    """
    Return all past decision records for a given incident_key,
    ordered oldest → newest (natural file order).
    """
    history = _load_history()
    return [h for h in history if h["incident_key"] == incident_key]


def get_history_count(incident_key: str) -> int:
    """Return how many past records exist for this incident_key."""
    return len(get_history(incident_key))


def save_decision(
    incident_key: str,
    action: str,
    confidence: float,
    safety_level: int,
    success: bool,
) -> None:
    """
    Persist a decision result after Workflow 3 / 4 executes the action.

    Fields stored:
      incident_key  – identifies the incident type+service pair
      action        – what was done
      confidence    – the *final* confidence that was used for this decision
      safety_level  – the *final* safety_level used (2 = manual, 3 = auto)
      success       – True if execution succeeded, False if it failed
      timestamp     – UTC time of recording
    """
    history = _load_history()

    history.append({
        "incident_key": incident_key,
        "action": action,
        "confidence": confidence,
        "safety_level": safety_level,
        "success": success,
        "timestamp": datetime.utcnow().isoformat(),
    })

    _save_history(history)
