from pydantic import BaseModel
from typing import Dict, Any
from datetime import datetime


class IncidentInput(BaseModel):
    incident_type: str
    service: str
    severity: str
    timestamp: datetime
    source: str


class DecisionOutput(BaseModel):
    decision_id: str
    incident_key: str
    action: str
    action_params: Dict[str, Any]
    safety_level: int
    confidence: float
    reason: str
    decided_at: datetime

