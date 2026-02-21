import json
from pathlib import Path
from typing import List, Dict, Any
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
    Return all past decisions for a given incident_key.
    """
    history = _load_history()
    return [h for h in history if h["incident_key"] == incident_key]


def save_decision(
    incident_key: str,
    action: str,
    confidence: float,
    safety_level: int,
    success: bool,
) -> None:
    """
    Persist a decision result.
    Called AFTER Workflow 3 executes the action.
    """

    history = _load_history()

    history.append({
        "incident_key": incident_key,
        "action": action,
        "confidence": confidence,
        "safety_level": safety_level,
        "success": success,
        "timestamp": datetime.utcnow().isoformat()
    })

    _save_history(history)

