import os
import jwt
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import subprocess

app = FastAPI(title="Action Executor")

# ── JWT configuration ────────────────────────────────────────────────────────
# SECRET_KEY is injected via ACTION_EXECUTOR_SECRET env var in docker-compose.
# The same key is used by n8n to sign the Bearer token it sends.
SECRET_KEY = os.getenv("ACTION_EXECUTOR_SECRET", "")
ALGORITHM = "HS256"

security = HTTPBearer()


def verify_jwt(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    FastAPI dependency — verifies the Bearer JWT on every protected endpoint.
    Returns 401 if the Authorization header is missing (HTTPBearer handles this).
    Returns 403 if the token signature is invalid or the token is malformed.
    """
    if not SECRET_KEY:
        raise HTTPException(status_code=500, detail="Server misconfiguration: secret not set")
    try:
        jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=403, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=403, detail="Invalid token")


# ── Action map ────────────────────────────────────────────────────────────────
class ExecuteRequest(BaseModel):
    decision_id: str
    action: str
    action_params: dict | None = {}
    safety_level: int | None = None
    confidence: float | None = None

ACTION_MAP = {
    "restart_redis":         ["docker", "restart", "redis"],
    "restart_redis_history": ["docker", "restart", "redis-history"],
    "restart_rabbitmq":      ["docker", "restart", "rabbitmq"],
    "restart_gateway":       ["docker", "restart", "gateway"],
    "restart_host":          ["reboot"],
    "cleanup_disk":          ["sh", "-c", "rm -rf /var/log/*"]
}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Public — no auth required. Used by Docker and monitoring."""
    return {"status": "ok"}


@app.post("/execute", dependencies=[Depends(verify_jwt)])
def execute(req: ExecuteRequest):
    """
    Protected — requires a valid JWT Bearer token.
    Called by n8n Workflow 3/4 to execute an approved action.
    """
    if req.action not in ACTION_MAP:
        raise HTTPException(status_code=400, detail=f"Unknown action: {req.action}")

    command = ACTION_MAP[req.action]
    result = subprocess.run(command, capture_output=True, text=True)

    status = "success" if result.returncode == 0 else "failed"

    return {
        "status": status,
        "decision_id": req.decision_id,
        "action": req.action,
        "return_code": result.returncode,
        "output": result.stdout if result.stdout else result.stderr,
    }
