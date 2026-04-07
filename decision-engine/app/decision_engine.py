import hashlib
import importlib
import uuid
from datetime import datetime, timezone

import rules as _rules_module
from models import IncidentInput, DecisionOutput
from history import get_history
from confidence import compute_confidence


def build_incident_key(incident: IncidentInput) -> str:
    """
    incident_key = SHA256(incident_type:service)

    - Timestamp is intentionally excluded so that repeated occurrences
      of the same incident type on the same service always share a single
      history, allowing confidence to accumulate across multiple alerts.
    - Example: "RedisDown:redis" → always the same hex key.
    """
    raw_key = f"{incident.incident_type}:{incident.service}"
    return hashlib.sha256(raw_key.encode()).hexdigest()


def make_decision(incident: IncidentInput) -> DecisionOutput:
    """
    Main decision function — called by app.py on POST /decide.

    Decision flow
    =============

    Step 1 – Build incident_key
        SHA256(incident_type:service)
        Same incident type on same service → always same key.

    Step 2 – Apply static rules
        Get base_confidence, base_safety_level, action, reason from rules.py.

    Step 3 – Load history
        Fetch all past records that share this incident_key.

    Step 4 – Branch on history
        ┌─ NO history ──────────────────────────────────────────────┐
        │  confidence   = base_confidence   (from rules.py)         │
        │  safety_level = base_safety_level (from rules.py)         │
        │  history_used = False                                      │
        └────────────────────────────────────────────────────────────┘
        ┌─ HAS history ──────────────────────────────────────────────┐
        │  confidence   = compute_confidence(base, history)          │
        │                 (weighted decay: recent results count more) │
        │  safety_level = 3 if confidence >= 0.7 else 2             │
        │                 (re-derived from learned confidence)        │
        │  history_used = True                                        │
        └─────────────────────────────────────────────────────────────┘

    Step 5 – Return DecisionOutput
        History saving is NOT done here — it is triggered by n8n Workflow 3/4
        calling POST /execution-result AFTER the action runs.
    """

    # 1. Build incident key
    incident_key = build_incident_key(incident)

    # 2. Apply static rules → always get base values
    # Reload rules module so docker-watcher's runtime additions are picked up
    importlib.reload(_rules_module)
    rule_result = _rules_module.evaluate_rules(
        incident_type=incident.incident_type,
        service=incident.service,
        severity=incident.severity,
    )

    # 3. Load history for this exact incident_key
    history = get_history(incident_key)

    # 4. Branch: no history vs learned history
    if not history:
        # ── BRANCH A: First time — use base values from rules ──────────────
        final_confidence = rule_result.base_confidence
        final_safety_level = rule_result.safety_level
        history_used = False

    else:
        # ── BRANCH B: History exists — learn and override ───────────────────
        final_confidence, history_used = compute_confidence(
            base_confidence=rule_result.base_confidence,
            history=history,
        )
        # Re-derive safety_level from learned confidence
        # (may upgrade SL2→3 after consistent successes, or demote SL3→2 after failures)
        final_safety_level = 3 if final_confidence >= 0.7 else 2

    # 5. Build and return decision
    return DecisionOutput(
        decision_id=str(uuid.uuid4()),
        incident_key=incident_key,
        action=rule_result.action,
        action_params=rule_result.action_params,
        safety_level=final_safety_level,
        confidence=final_confidence,
        reason=rule_result.reason,
        decided_at=datetime.now(timezone.utc).astimezone(),
        history_used=history_used,
    )
