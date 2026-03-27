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

import os
import time
import requests as _requests

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
    channel_id: str = ""

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

def _format_status(raw: str) -> str:
    """Reformat docker compose ps table into a clean readable list."""
    import re
    lines = raw.strip().splitlines()
    if len(lines) < 2:
        return "No containers found."
    header = lines[0]
    try:
        status_col = header.index("STATUS")
        ports_col  = header.index("PORTS")
    except ValueError:
        return raw  # fallback to raw if header unexpected
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        name        = line[:status_col].strip()
        status_text = line[status_col:ports_col].strip() if len(line) > status_col else ""
        ports_text  = line[ports_col:].strip()           if len(line) > ports_col  else ""
        if not name:
            continue
        icon = "🟢" if status_text.lower().startswith("up") else "🔴"
        # Shorten status: "Up 2 hours" → "Up 2h", "Up About a minute" → "Up ~1m"
        status_short = re.sub(r" hours?", "h", status_text)
        status_short = re.sub(r"About a minute", "~1m", status_short)
        status_short = re.sub(r"(\d+) minutes?", r"\1m", status_short)
        status_short = re.sub(r"(\d+) seconds?", r"\1s", status_short)
        # Extract first host port
        port_match = re.search(r'0\.0\.0\.0:(\d+)->', ports_text)
        port_info  = f"  :{port_match.group(1)}" if port_match else ""
        rows.append(f"{icon}  {name:<26}{status_short:<14}{port_info}")
    sep = "─" * 44
    header_line = f"{'NAME':<28}{'STATUS':<14}PORT"
    return f"🐳 *Container Status*  ({len(rows)} services)\n{sep}\n{header_line}\n{sep}\n" + "\n".join(rows) + f"\n{sep}"


def _yaml_preview(info) -> str:
    cport = info.container_port or info.port
    sep = "─" * 32
    lines = [
        f"*📋 Service Preview — {info.name}*",
        sep,
        f"  Image:   {info.image}",
        f"  Port:    {info.port} → {cport} (container)",
        f"  Probe:   {info.probe}",
        f"  Restart: {info.restart}",
        f"  Env:     {', '.join(info.env) if info.env else '—'}",
        f"  Volumes: {', '.join(info.volumes) if info.volumes else '—'}",
        f"  Depends: {', '.join(info.depends_on) if info.depends_on else '—'}",
        f"  Command: {info.command if info.command else '—'}",
        sep,
        "Confirm and apply?",
        "  *yes*  — deploy the service",
        "  *no*   — change the config",
        "  *cancel* — abort",
    ]
    return "\n".join(lines)


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
        return f"✗ docker compose up failed:\n{output}"
    docker_manager.wait_for_watcher(35)
    reloaded = cleanup_manager.reload_prometheus()
    sep = "─" * 30
    lines = [
        f"✅ Service '{info.name}' deployed!",
        sep,
        f"  Port:  {info.port}",
        f"  Image: {info.image}",
        "",
        "docker-watcher registered it in:",
        "  • decision-engine/app/rules.py",
        "  • action-executor/main.py",
        "  • prometheus/prometheus.yml",
        "  • prometheus/rules/auto-discovered.yml",
        "",
        f"{'✅' if reloaded else '✗'} Prometheus reloaded — pipeline active for '{info.name}'.",
        sep,
    ]
    return "\n".join(lines)


def _run_remove(service_name: str) -> str:
    results = cleanup_manager.remove_service(service_name)
    steps = {
        "container_stopped":   "Container stopped",
        "container_removed":   "Container removed",
        "compose_cleaned":     "docker-compose.yml cleaned",
        "rules_cleaned":       "decision-engine/rules.py cleaned",
        "action_cleaned":      "action-executor/main.py cleaned",
        "prometheus_cleaned":  "prometheus/prometheus.yml cleaned",
        "alerts_cleaned":      "auto-discovered.yml cleaned",
        "prometheus_reloaded": "Prometheus reloaded",
        "watcher_restarted":   "docker-watcher restarted",
    }
    all_ok = all(results.get(k) for k in (
        "compose_cleaned", "rules_cleaned", "action_cleaned",
        "prometheus_cleaned", "alerts_cleaned",
    ))
    sep = "─" * 30
    header = f"{'✅' if all_ok else '⚠'} Service '{service_name}' removal report:"
    lines = [header, sep]
    for key, label in steps.items():
        icon = "✓" if results.get(key) else "✗"
        lines.append(f"  {icon}  {label}")
    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Slack history cleaner
# ---------------------------------------------------------------------------

def clear_slack_history(channel_id: str) -> int:
    """
    Delete all messages in a Slack channel.
    - User messages  → deleted with SLACK_USER_TOKEN (xoxp-)
    - Bot messages   → deleted with SLACK_BOT_TOKEN  (xoxb-)
    Skips channel_join / channel_leave system messages.
    Returns the number of messages deleted.
    """
    user_token = os.getenv("SLACK_USER_TOKEN", "")
    bot_token  = os.getenv("SLACK_BOT_TOKEN", "")
    if not user_token or not channel_id:
        return 0

    user_headers = {"Authorization": f"Bearer {user_token}"}
    bot_headers  = {"Authorization": f"Bearer {bot_token}"} if bot_token else user_headers
    deleted = 0

    while True:
        resp = _requests.get(
            "https://slack.com/api/conversations.history",
            headers=user_headers,
            params={"channel": channel_id, "limit": 100},
            timeout=10,
        )
        data = resp.json()
        messages = data.get("messages", [])
        if not messages:
            break

        for msg in messages:
            if msg.get("subtype") in ("channel_join", "channel_leave"):
                continue
            # Bot messages have bot_id; use bot token to delete them
            is_bot = bool(msg.get("bot_id") or msg.get("subtype") == "bot_message")
            headers = bot_headers if is_bot else user_headers
            _requests.post(
                "https://slack.com/api/chat.delete",
                headers=headers,
                json={"channel": channel_id, "ts": msg["ts"]},
                timeout=10,
            )
            deleted += 1
            time.sleep(0.5)

        if not data.get("has_more"):
            break

    return deleted


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

    # ── Global commands (available at any step) ──────────────────────────────
    cmd = req.message.strip().lower()

    if cmd == "clear":
        clear_slack_history(req.channel_id)
        cm.reset()
        next_q = cm.current_question()
        combined = f"🧹 Chat cleared.\n\n{next_q}"
        return ChatResponse(session_id=req.session_id, reply="🧹 Chat cleared.",
                            next_question=combined, action_taken=False,
                            text=combined)

    if cmd == "reset":
        cm.reset()
        next_q = cm.current_question()
        combined = f"🔄 Conversation reset.\n\n{next_q}"
        return ChatResponse(session_id=req.session_id, reply="🔄 Conversation reset.",
                            next_question=combined, action_taken=False,
                            text=combined)

    if cmd in ("list", "services"):
        services = validator.get_existing_services()
        if services:
            reply = "📋 Services in docker-compose.yml:\n" + \
                    "\n".join(f"  • {s}" for s in services)
        else:
            reply = "No services found in docker-compose.yml."
        next_q = "" if cm.step == Step.ASK_INTENT else cm.current_question()
        combined = f"{reply}\n\n{next_q}".strip() if next_q else reply
        return ChatResponse(session_id=req.session_id, reply=reply,
                            next_question=combined, action_taken=False,
                            text=combined)

    if cmd == "status":
        output = docker_manager.get_status()
        reply = _format_status(output)
        next_q = "" if cm.step == Step.ASK_INTENT else cm.current_question()
        combined = f"{reply}\n\n{next_q}".strip() if next_q else reply
        return ChatResponse(session_id=req.session_id, reply=reply,
                            next_question=combined, action_taken=False,
                            text=combined)

    if cmd in ("quit", "exit", "bye"):
        cm.reset()
        goodbye = "👋 Goodbye! Type 'add' or 'remove' to start a new session."
        return ChatResponse(session_id=req.session_id, reply=goodbye,
                            next_question=goodbye, action_taken=False,
                            text=goodbye)

    # Normalize skip aliases so Slack users can type -, ., none, n/a instead
    # of pressing Enter (which is impossible in Slack).  Only affects optional
    # steps — required-field steps reject "skip" with their own error message.
    _SKIP_WORDS = {"-", "--", ".", "none", "n/a", "na", "nope", "skip"}
    message_to_process = "skip" if req.message.strip().lower() in _SKIP_WORDS else req.message

    # process() always returns (step, error_message)
    step, error = cm.process(message_to_process)

    # Validation failed — return the error and repeat the current question
    if error:
        reply_text = f"⚠ {error}"
        next_q = cm.current_question()
        combined = f"{reply_text}\n\n{next_q}".strip()
        return ChatResponse(
            session_id=req.session_id,
            reply=reply_text,
            next_question=combined,
            action_taken=False,
            text=combined,
        )

    reply = ""
    action_taken = False

    # next_q is appended unless the reply already contains the prompt
    # or the session just completed an action (no automatic greeting)
    suppress_next_q = False

    if step == Step.READY_TO_ADD:
        reply = _run_add(cm.service_info)
        action_taken = True
        cm.reset()
        suppress_next_q = True   # don't auto-show greeting after deploy

    elif step == Step.READY_TO_REMOVE:
        reply = _run_remove(cm.remove_target)
        action_taken = True
        cm.reset()
        suppress_next_q = True   # don't auto-show greeting after removal

    elif step == Step.ADD_CONFIRM:
        reply = _yaml_preview(cm.service_info)
        suppress_next_q = True   # confirm prompt already embedded in preview

    elif step == Step.REMOVE_ASK_CONFIRM:
        locs = cm.remove_locations
        found_in = [f for f, v in locs.items() if v]
        not_in   = [f for f, v in locs.items() if not v]
        sep = "─" * 30
        lines = [f"🔍 Service '{cm.remove_target}' found in:", sep]
        for f in found_in:
            lines.append(f"  ✓  {f}")
        if not_in:
            lines.append(f"  —  Not in: {', '.join(not_in)}")
        lines.append(sep)
        reply = "\n".join(lines)

    next_q = "" if suppress_next_q else cm.current_question()
    combined = f"{reply}\n\n{next_q}".strip() if (reply and next_q) else (reply or next_q)

    return ChatResponse(
        session_id=req.session_id,
        reply=reply,
        next_question=combined,
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
