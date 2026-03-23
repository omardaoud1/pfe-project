"""
api.py — FastAPI HTTP wrapper for DockerAgent.

Endpoints:
  POST /chat   — send a message, get a reply + next question
  POST /reset  — reset a session
  GET  /health — liveness check

Session flow matches agent.py exactly — step-by-step, one question at a time.
session_id is the caller's unique ID (phone number for WhatsApp).

Run:
  uvicorn api:app --host 0.0.0.0 --port 8002 --reload
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

from conversation_manager import ConversationManager, Step
import validator
import docker_manager
import cleanup_manager

app = FastAPI(title="DockerAgent API", version="1.0.0")

# ---------------------------------------------------------------------------
# Session store  (capped at MAX_SESSIONS to prevent unbounded memory growth)
# ---------------------------------------------------------------------------
_sessions: dict[str, ConversationManager] = {}
MAX_SESSIONS = 200


def get_session(session_id: str) -> ConversationManager:
    if session_id not in _sessions:
        if len(_sessions) >= MAX_SESSIONS:
            # Evict the oldest session (dicts are insertion-ordered in Python 3.7+)
            oldest = next(iter(_sessions))
            del _sessions[oldest]
        _sessions[session_id] = ConversationManager()
    return _sessions[session_id]


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    session_id: str
    message: str

class ChatResponse(BaseModel):
    session_id: str
    reply: str            # agent's reply / error / result message
    next_question: str    # next question to ask the user (empty if done)
    action_taken: bool    # True if a docker action was executed
    text: str = ""        # combined reply + next_question for WhatsApp (n8n reads this)


class ResetRequest(BaseModel):
    session_id: str

class ResetResponse(BaseModel):
    session_id: str
    next_question: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_add(info) -> str:
    docker_manager.add_service(
        name=info.name,
        image=info.image,
        port=info.port,
        container_port=info.container_port or None,
        probe=info.probe,
        restart=info.restart,
        env=info.env or None,
        volumes=info.volumes or None,
        depends_on=info.depends_on or None,
        command=info.command or None,
    )
    success, output = docker_manager.compose_up(info.name)
    if not success:
        cleanup_manager.remove_from_compose(info.name)
        return f"docker compose up failed: {output}"
    docker_manager.wait_for_watcher(35)
    cleanup_manager.reload_prometheus()
    return (
        f"Service '{info.name}' is running on port {info.port}. "
        f"docker-watcher has registered it in all config files. "
        f"Prometheus reloaded. The monitoring pipeline is now active for '{info.name}'."
    )


def _run_remove(service_name: str) -> str:
    results = cleanup_manager.remove_service(service_name)
    steps = {
        "container_stopped":   "Container stopped",
        "container_removed":   "Container removed",
        "compose_cleaned":     "docker-compose.yml",
        "rules_cleaned":       "decision-engine/rules.py",
        "action_cleaned":      "action-executor/main.py",
        "prometheus_cleaned":  "prometheus/prometheus.yml",
        "alerts_cleaned":      "auto-discovered.yml",
        "prometheus_reloaded": "Prometheus reloaded",
    }
    lines = [f"Service '{service_name}' removal report:"]
    for key, label in steps.items():
        icon = "✓" if results.get(key) else "✗"
        lines.append(f"  {icon}  {label}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "service": "docker-agent"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    cm = get_session(req.session_id)

    # process() always returns (step, error_message)
    step, error = cm.process(req.message)

    # Validation failed — return the error and repeat the current question
    if error:
        reply_text = f"⚠ {error}"
        next_q = cm.current_question()
        combined = f"{reply_text}\n\n{next_q}".strip() if reply_text else next_q
        return ChatResponse(
            session_id=req.session_id,
            reply=reply_text,
            next_question=next_q,
            action_taken=False,
            text=combined,
        )

    reply = ""
    action_taken = False

    if step == Step.READY_TO_ADD:
        reply = _run_add(cm.service_info)
        action_taken = True
        cm.reset()

    elif step == Step.READY_TO_REMOVE:
        reply = _run_remove(cm.remove_target)
        action_taken = True
        cm.reset()

    elif step == Step.REMOVE_ASK_CONFIRM:
        locs = cm.remove_locations
        found_in = [f for f, v in locs.items() if v]
        not_in   = [f for f, v in locs.items() if not v]
        lines = [f"⚠ Service '{cm.remove_target}' found in:"]
        for f in found_in:
            lines.append(f"  ✓  {f}")
        if not_in:
            lines.append(f"  —  Not registered in: {', '.join(not_in)}")
        reply = "\n".join(lines)

    next_q = cm.current_question()
    combined = f"{reply}\n\n{next_q}".strip() if reply else next_q

    return ChatResponse(
        session_id=req.session_id,
        reply=reply,
        next_question=next_q,
        action_taken=action_taken,
        text=combined,
    )


@app.post("/reset", response_model=ResetResponse)
def reset(req: ResetRequest):
    if req.session_id in _sessions:
        _sessions[req.session_id].reset()
    else:
        _sessions[req.session_id] = ConversationManager()
    return ResetResponse(
        session_id=req.session_id,
        next_question=_sessions[req.session_id].current_question(),
    )
