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

# Static map for non-restart actions only.
# restart_* actions are resolved dynamically — no entry needed here for new services.
ACTION_MAP = {
    "restart_host":  ["reboot"],
    "cleanup_disk":  ["sh", "-c", "rm -rf /var/log/*"],
    "restart_docker_agent":         ["docker", "restart", "docker-agent"],
    "restart_test_agent":         ["docker", "restart", "test-agent"],
    "restart_evolution_postgres":         ["docker", "restart", "evolution-postgres"],
    "restart_evolution_api":         ["docker", "restart", "evolution-api"],
    "restart_ngrok":         ["docker", "restart", "ngrok"],
}


def resolve_command(action: str) -> list:
    """
    Resolve an action string to a shell command.

    For restart_* actions: derive the container name from the action key.
    docker-watcher generates action keys as restart_<name_with_underscores>
    for containers named <name-with-hyphens>. We try both forms so any
    auto-discovered service works without any static map entry.

    For other actions: look up ACTION_MAP.
    """
    if action.startswith("restart_"):
        service_part = action[len("restart_"):]
        # Try hyphenated form first (docker container names use hyphens),
        # then underscore form as fallback.
        for candidate in [service_part.replace("_", "-"), service_part]:
            probe = subprocess.run(
                ["docker", "inspect", "--format", "{{.Name}}", candidate],
                capture_output=True, text=True
            )
            if probe.returncode == 0:
                return ["docker", "restart", candidate]
        raise HTTPException(
            status_code=400,
            detail=f"No container found for action '{action}' "
                   f"(tried: {service_part.replace('_', '-')!r}, {service_part!r})"
        )

    if action in ACTION_MAP:
        return ACTION_MAP[action]

    raise HTTPException(status_code=400, detail=f"Unknown action: {action}")


@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/execute")
def execute(req: ExecuteRequest):
    command = resolve_command(req.action)

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

