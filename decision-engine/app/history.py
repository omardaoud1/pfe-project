import json
import os
import redis
from typing import List, Dict, Any
from datetime import datetime, timezone

import logging

logger = logging.getLogger(__name__)

# ── Redis connection ─────────────────────────────────────────────────────────
# Points to redis-history (port 6380 externally, 6379 inside Docker network).
# decode_responses=True → Redis returns str instead of bytes.
# socket_connect_timeout=2 → fail fast if Redis is unreachable.
_REDIS_URL = os.getenv("REDIS_URL", "redis://redis-history:6379/0")
_r = redis.from_url(_REDIS_URL, decode_responses=True, socket_connect_timeout=2)

# Maximum number of actions kept per incident_key
MAX_HISTORY = 30

# Key prefix used in Redis to namespace decision-history lists
_KEY_PREFIX = "decision_history:"


def _redis_key(incident_key: str) -> str:
    return f"{_KEY_PREFIX}{incident_key}"


# ── Public API (same interface as the old JSON-based history.py) ─────────────

def get_history(incident_key: str) -> List[Dict[str, Any]]:
    """
    Return the last MAX_HISTORY (30) decision records for this incident_key,
    ordered oldest → newest (index 0 = oldest, -1 = newest).

    Returns [] silently if Redis is unreachable so /decide never crashes.
    """
    try:
        raw_entries = _r.lrange(_redis_key(incident_key), 0, MAX_HISTORY - 1)
        # raw_entries is newest-first (LPUSH); reverse to oldest-first for confidence.py
        return [json.loads(entry) for entry in reversed(raw_entries)]
    except Exception as exc:
        logger.warning("Redis unavailable – returning empty history: %s", exc)
        return []


def get_all_keys() -> list:
    """
    Return all incident_keys that currently have stored history in Redis.
    Strips the 'decision_history:' prefix so only the raw SHA256 hash is returned.
    """
    try:
        keys = _r.keys(f"{_KEY_PREFIX}*")
        return [k[len(_KEY_PREFIX):] for k in keys]
    except Exception as exc:
        logger.warning("Redis unavailable – cannot list keys: %s", exc)
        return []


def get_all_history() -> List[Dict[str, Any]]:
    """
    Return ALL stored decision entries across every incident_key,
    sorted oldest → newest by timestamp.
    This is the Redis equivalent of reading the full decision_history.json.
    """
    all_entries = []
    for incident_key in get_all_keys():
        all_entries.extend(get_history(incident_key))
    # Sort globally by timestamp so the list reads chronologically
    all_entries.sort(key=lambda e: e.get("timestamp", ""))
    return all_entries


def get_history_count(incident_key: str) -> int:
    """Return how many past records exist for this incident_key (max 30)."""
    return _r.llen(_redis_key(incident_key))


def save_decision(
    incident_key: str,
    action: str,
    confidence: float,
    safety_level: int,
    success: bool,
) -> None:
    """
    Persist a decision result into Redis.

    Strategy: LPUSH + LTRIM
      - LPUSH  → push new entry to the LEFT (head) of the list  [newest first]
      - LTRIM  → keep only positions 0 … MAX_HISTORY-1          [auto-evict oldest]

    This is atomic and guarantees the list never exceeds MAX_HISTORY entries.

    Fields stored:
      incident_key  – identifies the incident type+service pair
      action        – what was done
      confidence    – the *final* confidence used for this decision
      safety_level  – the *final* safety_level used (2 = manual, 3 = auto)
      success       – True if execution succeeded, False if it failed
      timestamp     – UTC time of recording
    """
    entry = {
        "incident_key": incident_key,
        "action": action,
        "confidence": confidence,
        "safety_level": safety_level,
        "success": success,
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
    }

    key = _redis_key(incident_key)
    try:
        _r.lpush(key, json.dumps(entry))      # push newest to head
        _r.ltrim(key, 0, MAX_HISTORY - 1)     # keep only the last 30
    except Exception as exc:
        logger.warning("Redis unavailable – decision NOT persisted: %s", exc)
