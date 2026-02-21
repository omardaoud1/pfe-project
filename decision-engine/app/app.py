from pydantic import BaseModel
from history import save_decision
from fastapi import FastAPI, HTTPException
from models import IncidentInput, DecisionOutput
from decision_engine import make_decision

app = FastAPI(
    title="Decision Engine API",
    description="Decision service for incident-driven automation",
    version="1.0.0"
)


@app.post("/decide", response_model=DecisionOutput)
def decide(incident: IncidentInput):
    """
    Called by n8n Workflow 2.
    Receives an incident and returns a final decision.
    """
    try:
        decision = make_decision(incident)
        return decision

    except Exception as e:
        # Any unexpected error must be explicit for n8n
        raise HTTPException(
            status_code=500,
            detail=f"Decision engine error: {str(e)}"
        )

class ExecutionResultInput(BaseModel):
    incident_key: str
    action: str
    confidence: float
    safety_level: int
    execution_status: str  # "success" | "failed"


@app.post("/execution-result")
def execution_result(data: ExecutionResultInput):
    """
    Called by n8n Workflow 3 (Node 5A).
    Persists execution result into decision_history.json
    """

    save_decision(
        incident_key=data.incident_key,
        action=data.action,
        confidence=data.confidence,
        safety_level=data.safety_level,
        success=(data.execution_status.lower () == "success")
    )

    return {"status": "stored"}
