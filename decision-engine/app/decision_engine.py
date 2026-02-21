import hashlib
from datetime import datetime
import uuid

from models import IncidentInput, DecisionOutput
from rules import evaluate_rules
from history import get_history
from confidence import compute_confidence


def build_incident_key(incident: IncidentInput) -> str:
    """
    incident_key = hash(incident_type + service + timestamp)
    """
    raw_key = f"{incident.incident_type}:{incident.service}:{incident.timestamp.isoformat()}"
    return hashlib.sha256(raw_key.encode()).hexdigest()


def make_decision(incident: IncidentInput) -> DecisionOutput:
    """
    Main decision function.
    Called by app.py.
    """

    # 1. Build incident key
    incident_key = build_incident_key(incident)

    # 2. Apply static rules
    rule_result = evaluate_rules(
        incident_type=incident.incident_type,
        service=incident.service,
        severity=incident.severity
    )

    # 3. Load history
    history = get_history(incident_key)

    # 4. Compute confidence
    final_confidence = compute_confidence(
        base_confidence=rule_result.base_confidence,
        history=history
    )

    # 5. Build decision
    decision = DecisionOutput(
        decision_id=str(uuid.uuid4()),
        incident_key=incident_key,
        action=rule_result.action,
        action_params=rule_result.action_params,
        safety_level=rule_result.safety_level,
        confidence=final_confidence,
        reason=rule_result.reason,
        decided_at=datetime.utcnow()
    )

    return decision

