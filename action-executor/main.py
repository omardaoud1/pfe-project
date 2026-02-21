from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess

app = FastAPI(title="Action Executor")

class ExecuteRequest(BaseModel):
    decision_id: str
    action: str
    action_params: dict | None = {}
    safety_level: int | None = None
    confidence: float | None = None

ACTION_MAP = {
    "restart_redis": ["docker", "restart", "redis"],
    "restart_rabbitmq": ["docker", "restart", "rabbitmq"],
    "restart_gateway": ["docker", "restart", "gateway"],
    "restart_host": ["reboot"],
    "cleanup_disk": ["sh", "-c", "rm -rf /var/log/*"]
}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/execute")
def execute(req: ExecuteRequest):
    if req.action not in ACTION_MAP:
        raise HTTPException(status_code=400, detail="Unknown action")

    command = ACTION_MAP[req.action]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True
    )

    status = "success" if result.returncode == 0 else "failed"

    return {
        "status": status,
        "decision_id": req.decision_id,
        "action": req.action,
        "return_code": result.returncode,
        "output": result.stdout if result.stdout else result.stderr
    }

