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
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

from conversation_manager import ConversationManager, Step
import validator
import docker_manager
import cleanup_manager
import health_reporter
import report_formatter
import log_analyzer
import log_parser
import llm_client as _llm

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


def _run_log_analysis(service: str, tail: int = 100) -> str:
    """Fetch logs and run complete rule-based analysis — instant, no LLM."""
    import re as _re

    raw_logs, error = log_analyzer.get_logs(service, tail=tail)
    if error:
        return f"⚠ {error}"

    _ts = _re.compile(r'^\d{4}-\d{2}-\d{2}T[\d:\.]+Z\s+')
    lines = [_ts.sub("", l).strip() for l in raw_logs.splitlines() if l.strip()]

    if not lines:
        sep = "─" * 36
        return (
            f"🔍 *Log Analysis — {service}* (last {tail} lines)\n{sep}\n"
            f"*What happened:* The service produced no log output in the last {tail} lines.\n"
            f"This usually means one of two things:\n"
            f"  • The service is idle and healthy — it simply has nothing to report.\n"
            f"  • The service is completely frozen or unresponsive and stopped logging.\n\n"
            f"*What to do:* Check if the container is actually running with `docker ps`. "
            f"If it is running, the service is likely healthy and quiet.\n{sep}"
        )

    errors   = [l for l in lines if _re.search(
        r'\b(ERROR|CRITICAL|FATAL|EXCEPTION|TRACEBACK|panic)\b', l, _re.IGNORECASE)]
    warnings = [l for l in lines if _re.search(
        r'\b(WARN|WARNING|DEPRECATED)\b', l, _re.IGNORECASE)
        and not _re.search(r'\b(ERROR|CRITICAL|FATAL)\b', l, _re.IGNORECASE)]

    full = "\n".join(lines).lower()

    # ── Startup vs runtime failure detection ─────────────────────────────────
    # Errors in the first 20% of lines = startup failure
    startup_lines = lines[:max(1, len(lines) // 5)]
    startup_full  = "\n".join(startup_lines).lower()
    startup_failed = bool(errors and any(e in startup_full for e in
                          [l.lower() for l in errors[:3]]))

    # ── Exit code translation ────────────────────────────────────────────────
    # Only match explicit "exited with code N" or "exit code N" — never loose "exit\nYYYY"
    exit_code = None
    _ec = _re.search(
        r'exited?\s+with\s+(?:exit\s+)?code\s+(\d+)'   # "exited with code 0"
        r'|exit\s+code[:\s]+(\d+)',                      # "exit code: 1"
        full
    )
    if _ec:
        raw = _ec.group(1) or _ec.group(2)
        _v  = int(raw)
        exit_code = _v if _v <= 255 else None  # ignore false positives > 255 (e.g. years)

    # ── Context extractor: N lines before/after a matching line ──────────────
    def _context(match_lines: list[str], before: int = 1, after: int = 1) -> list[str]:
        """Return unique context windows around matched lines."""
        idxs = set()
        for ml in match_lines[:3]:
            for i, l in enumerate(lines):
                if ml in l:
                    for j in range(max(0, i - before), min(len(lines), i + after + 1)):
                        idxs.add(j)
                    break
        return [lines[i] for i in sorted(idxs)]

    # ── Findings: (emoji, what_happened, what_to_do) ─────────────────────────
    findings = []

    # 1. OOM — Docker memory limit (config) vs kernel OOM kill (exit 137)
    if exit_code == 137 or _re.search(r'oom.kill|killed by.*kernel|out of memory kill', full):
        findings.append((
            "🧠",
            f"`{service}` was forcefully killed by the Linux kernel's OOM killer (exit code 137). "
            "The kernel kills processes when the entire host runs out of RAM — not just this container. "
            "This is more severe than a Docker memory limit: it means the whole machine was under memory pressure.",
            "The host itself is running low on memory. "
            "Reduce memory usage across all services, or add more RAM. "
            f"You can also set a `mem_limit` in docker-compose.yml for `{service}` "
            "to prevent it from consuming too much."
        ))
    elif _re.search(r'out of memory|oom killer|memory limit exceeded', full):
        findings.append((
            "🧠",
            f"`{service}` ran out of memory and was killed. "
            "The service exceeded its configured memory limit and was terminated. "
            "The process ended abruptly with no graceful shutdown.",
            f"Increase the `mem_limit` for `{service}` in docker-compose.yml "
            "(e.g. `mem_limit: 512m`) and restart."
        ))

    # 2. Connection refused — extract target host:port if possible
    if _re.search(r'connection refused|econnrefused|failed to connect|cannot connect|no route to host', full):
        host_m = _re.search(r'(?:connect(?:ing)?|dial(?:ing)?|reach)\s+(?:to\s+)?([a-z0-9][\w.-]+):(\d+)', full)
        target = f"{host_m.group(1)}:{host_m.group(2)}" if host_m else "a dependency"
        phase  = "during startup" if startup_failed else "while running"
        findings.append((
            "🔌",
            f"`{service}` could not connect to `{target}` {phase}. "
            "The destination is either not running, not yet ready, "
            "or the hostname/port is misconfigured.",
            f"Verify that `{target.split(':')[0]}` is running and reachable. "
            "Check the environment variables for hostname and port settings. "
            "If it's a startup issue, make sure dependencies start before this service "
            "using `depends_on` in docker-compose.yml."
        ))

    # 3. Timeout
    if _re.search(r'\btimeout\b|timed out|deadline exceeded|context deadline', full):
        findings.append((
            "⏱",
            f"`{service}` waited too long for a response from an upstream service and timed out. "
            "The upstream is either overloaded, too slow, or unreachable.",
            "Check if the upstream service is healthy and responsive. "
            "If it is under heavy load, consider scaling it or increasing the timeout value "
            "in the service configuration."
        ))

    # 4. Permission denied
    if _re.search(r'permission denied|eacces|access denied|operation not permitted', full):
        path_m = _re.search(r'(?:open|read|write|access|stat)\s+["\']?(/[\w./-]+)', full)
        path   = f" (`{path_m.group(1)}`)" if path_m else ""
        findings.append((
            "🔒",
            f"`{service}` was denied access to a file or resource{path}. "
            "The process does not have the OS-level permission to read, write, or execute it.",
            "Check the volume mount permissions in docker-compose.yml. "
            "The file or directory must be owned by the user the container runs as. "
            "You may need to `chmod` or `chown` the host path."
        ))

    # 5. Disk full
    if _re.search(r'no space left|disk full|enospc|quota exceeded', full):
        findings.append((
            "💾",
            "The host disk is completely full. "
            f"`{service}` could not write data and will fail until space is freed. "
            "This affects every service that tries to write to disk.",
            "Free disk space immediately: remove unused Docker images (`docker image prune -a`), "
            "stopped containers (`docker container prune`), and dangling volumes. "
            "Then restart the service."
        ))

    # 6. TLS / certificate
    if _re.search(r'x509|certificate.*expired|tls.*handshake|ssl.*error|cert.*invalid|certificate.*verify', full):
        findings.append((
            "🔐",
            f"`{service}` failed a TLS/SSL handshake. "
            "The certificate it received is either expired, self-signed without a trusted CA, "
            "or the hostname in the certificate does not match the server address.",
            "Check the certificate expiry date. "
            "If using self-signed certificates, add the CA to the container's trusted store. "
            "If the cert is expired, renew it and restart the affected service."
        ))

    # 7. Graceful shutdown vs crash vs restart loop
    # Graceful: explicit SIGTERM, OR (SIGCHLD + worker exit 0 + clean "exit" line)
    _graceful = bool(
        _re.search(r'sigterm|signal 15\b|graceful.*shutdown|received.*shutdown', full) or
        (
            _re.search(r'sigchld|signal 17\b|worker.*exited|worker.*process.*exit', full)
            and exit_code in (None, 0)
            and not _re.search(r'segfault|core dumped|signal 11\b|abort|panic:', full)
        )
    )
    # Crash: hard signals or non-zero exit — but NEVER override a confirmed graceful stop
    _crash = bool(
        not _graceful and (
            _re.search(r'segfault|core dumped|signal 11\b|abort|panic:', full) or
            (exit_code is not None and exit_code not in (0, 137))
        )
    )

    if _crash and not any(f[0] == "🧠" for f in findings):
        last_err = f" Last error: \"{errors[-1][:120]}\"." if errors else ""
        exit_meaning = {
            1:   "general error (the app itself reported a failure)",
            2:   "misuse of shell or command (wrong arguments or config syntax error)",
            126: "permission problem — the binary cannot be executed",
            127: "command not found — the entrypoint binary is missing in the image",
            130: "interrupted by Ctrl+C (SIGINT)",
            143: "terminated by SIGTERM (same as graceful stop)",
        }.get(exit_code, f"exit code {exit_code}")
        findings.append((
            "💥",
            f"`{service}` crashed with {exit_meaning}.{last_err} "
            + ("This happened at startup — the service failed before it could become ready. "
               if startup_failed else
               "This happened during normal operation — the service was running and then failed. "),
            "Read the key log lines below carefully — they contain the direct cause. "
            "Fix the reported error, then restart the service."
        ))
    elif _graceful and not _crash:
        findings.append((
            "⏹",
            f"`{service}` was stopped gracefully. "
            "It received a SIGTERM signal (sent by `docker stop` or a system restart), "
            "finished in-progress work, shut all worker processes down cleanly "
            "with exit code 0, and exited normally. No errors occurred.",
            f"The service is stopped intentionally. "
            f"To bring it back: `docker compose up -d {service}`"
        ))
    elif _re.search(r'\brestart\b', full) and not _graceful:
        last_err = f" Last error before restart: \"{errors[-1][:120]}\"." if errors else ""
        findings.append((
            "🔄",
            f"`{service}` is in a restart loop — it crashes on startup and Docker keeps restarting it.{last_err} "
            "This almost always means a misconfiguration, a missing dependency, "
            "or the service cannot reach something it needs to start.",
            "The error lines below show exactly why it is failing. "
            "Fix the root cause (wrong env var, missing file, dependency not ready), "
            "then restart."
        ))

    # 8. Port conflict
    if _re.search(r'address already in use|eaddrinuse|bind.*failed', full):
        port_m = _re.search(r'[:\s](\d{2,5})\b', full)
        port   = port_m.group(1) if port_m else "a port"
        findings.append((
            "🔁",
            f"`{service}` could not start because port {port} is already in use by another process. "
            "Two services cannot listen on the same port simultaneously.",
            f"Find which service owns port {port} and stop it, "
            f"or change the host port mapping for `{service}` in docker-compose.yml."
                    ))

    # 9. Database schema / migration
    if _re.search(r'relation .* does not exist|no such table|migration.*fail|schema.*not.*found|column .* does not exist', full):
        findings.append((
            "🗄",
            f"`{service}` tried to query a database table or column that does not exist. "
            "The database schema is out of sync — migrations have not been run, "
            "were run on the wrong database, or an old schema version is being used.",
            "Run the database migrations for this service. "
            "Make sure the database container started before this service."
        ))

    # 10. Authentication failure
    if _re.search(r'authentication failed|invalid.*password|wrong.*credential|unauthorized|401 unauthorized|access denied.*password', full):
        findings.append((
            "🔑",
            f"`{service}` failed to authenticate against a database, API, or external service. "
            "The credentials it used were rejected — wrong password, expired token, or invalid API key.",
            "Check the environment variables for this service in docker-compose.yml or .env. "
            "Make sure the password/token matches what the target service expects."
        ))

    # 11. Service-specific patterns ───────────────────────────────────────────

    # Nginx
    if _re.search(r'\bnginx\b', full):
        if _re.search(r'config.*test.*failed|configuration.*error|invalid.*directive|syntax error', full):
            findings.append((
                "⚙️",
                "Nginx has a configuration syntax error. "
                "It refused to start because the config file failed validation.",
                "Fix the nginx configuration file. "
                "Test it manually with: `nginx -t` inside the container."
            ))
        if _re.search(r'bind\(\) to.*failed.*permission', full):
            findings.append((
                "🔒",
                "Nginx could not bind to port 80 or 443 because it lacks permission. "
                "Inside Docker, binding to ports below 1024 sometimes requires elevated privileges.",
                "Add `cap_add: [NET_BIND_SERVICE]` to the service in docker-compose.yml, "
                "or run nginx on a port above 1024 and use a host port mapping."
            ))
        if _re.search(r'upstream timed out|connect\(\) failed.*upstream|502 bad gateway|no live upstreams|upstream connect error', full):
            upstream_m = _re.search(r'upstream\s+"?([^"]+?)"?\s*(?:while|,)', full)
            upstream   = f" (`{upstream_m.group(1).strip()}`)" if upstream_m else ""
            findings.append((
                "🌐",
                f"Nginx received a 502/504 — it could not reach the upstream service{upstream}. "
                "The backend server is either down, too slow to respond, or refused the connection. "
                "This means real users are seeing gateway errors right now.",
                f"Check the upstream service logs immediately. "
                "Verify it is running and accepting connections. "
                "If it is overloaded, scale it up or reduce the request rate."
            ))

    # Redis
    if _re.search(r'\bredis\b', full):
        if _re.search(r'loading.*dataset|rdb.*loading|aof.*loading', full):
            findings.append((
                "💿",
                "Redis is loading its persisted dataset from disk on startup. "
                "This is normal after a restart — it replays the RDB or AOF file "
                "to restore the previous state. The service will be ready once loading finishes.",
                "No action needed. Wait for Redis to finish loading. "
                "For large datasets this can take a minute."
            ))
        if _re.search(r'background.*saving.*error|rdb.*write.*error|aof.*write.*error', full):
            findings.append((
                "💾",
                "Redis failed to save its data to disk. "
                "The persistence write (RDB snapshot or AOF append) failed, "
                "which means data may be lost if the container restarts.",
                "Check available disk space and file permissions on the Redis data volume. "
                "Make sure the volume mount in docker-compose.yml is writable."
            ))
        if _re.search(r'maxmemory.*policy|evicting|max memory reached', full):
            findings.append((
                "🧠",
                "Redis hit its `maxmemory` limit and is evicting keys according to its policy. "
                "This means Redis is under memory pressure and actively removing data.",
                "Increase the `maxmemory` value in the Redis configuration, "
                "or reduce the amount of data being stored."
            ))
        if _re.search(r'no config file specified|using the default config', full):
            findings.append((
                "⚙️",
                f"`{service}` started without a configuration file and is using built-in defaults. "
                "This is not an error — Redis is running — but the defaults are unsafe for production: "
                "no password, no memory limit, no persistence policy, and it binds to all interfaces.",
                "Create a `redis.conf` file and mount it into the container. "
                "Minimum production settings:\n"
                "  • `requirepass yourStrongPassword` — require authentication\n"
                "  • `maxmemory 512mb` — cap memory usage\n"
                "  • `maxmemory-policy allkeys-lru` — eviction when full\n"
                "  • `bind 127.0.0.1` — restrict network access\n"
                "  • `save 60 1` — enable RDB snapshots\n"
                "Mount it in docker-compose.yml: `./redis.conf:/usr/local/etc/redis/redis.conf`"
            ))
        if _re.search(r'replication timeout|replica.*timeout|lost connection to master|master is down|cannot connect to master', full):
            findings.append((
                "🔁",
                f"`{service}` lost its replication connection to the Redis master. "
                "The replica is no longer receiving updates — reads from this replica will return stale data. "
                "If this is the session/cache Redis, users may see inconsistent data.",
                "Check if the master Redis is running and reachable from this container. "
                "Look at master logs for signs of overload or crash. "
                "The replica will attempt to reconnect automatically."
            ))
        if _re.search(r'sentinel|failover.*start|elected.*master|switch.*master|master.*changed', full):
            findings.append((
                "🔀",
                "Redis Sentinel detected a master failure and initiated a failover. "
                "A replica has been promoted to master. "
                "During the failover window (usually 10-30 seconds) write operations fail.",
                "Verify the new master is healthy and all replicas have reconnected. "
                "Check that your application's Redis client supports Sentinel and reconnects automatically. "
                "Investigate why the original master failed."
            ))
        if _re.search(r'max number of clients|maxclients|too many connections', full):
            findings.append((
                "🔗",
                f"`{service}` has reached its `maxclients` limit. "
                "New connection attempts are being rejected. "
                "This happens when too many services or workers hold open Redis connections simultaneously.",
                "Increase `maxclients` in redis.conf (default is 10000). "
                "More importantly, audit connection pool sizes in services that connect to this Redis — "
                "use connection pooling instead of creating a new connection per request."
            ))

    # PostgreSQL
    if _re.search(r'\bpostgres\b|\bpostgresql\b|\bpg\b', full):
        if _re.search(r'max_connections|too many clients|remaining connection slots', full):
            findings.append((
                "🔗",
                "PostgreSQL has reached its maximum number of connections. "
                "New connection attempts are being rejected because the connection pool is full.",
                "Increase `max_connections` in the PostgreSQL config, "
                "or use a connection pooler like PgBouncer in front of the database."
            ))
        if _re.search(r'could not open file|data directory.*permission|could not create.*directory', full):
            findings.append((
                "🔒",
                "PostgreSQL cannot access its data directory. "
                "The data folder is either missing, has wrong permissions, "
                "or is owned by a different user than the postgres process.",
                "Check the volume mount for the PostgreSQL data directory in docker-compose.yml. "
                "The directory must be owned by UID 999 (the default postgres user)."
            ))
        if _re.search(r'deadlock detected|deadlock found', full):
            findings.append((
                "🔒",
                "PostgreSQL detected a deadlock — two transactions were each waiting for the other to "
                "release a lock. PostgreSQL automatically killed one of them to break the cycle. "
                "The killed transaction was rolled back and must be retried by the application.",
                "Find which queries/tables are involved using `pg_stat_activity` and `pg_locks`. "
                "Ensure all code acquires locks in a consistent order. "
                "Frequent deadlocks indicate a race condition in the application logic."
            ))
        if _re.search(r'statement timeout|query.*canceled|long.running query|duration:.*ms', full):
            dur_m = _re.search(r'duration:\s*([\d.]+)\s*ms', full)
            dur   = f" ({float(dur_m.group(1))/1000:.1f}s)" if dur_m else ""
            findings.append((
                "⏱",
                f"PostgreSQL recorded a slow or timed-out query{dur}. "
                "A query ran longer than the configured `statement_timeout` and was cancelled, "
                "or it is simply very slow and blocking other operations.",
                "Run `EXPLAIN ANALYZE` on the slow query to find missing indexes. "
                "Check `pg_stat_activity` for currently running long queries. "
                "Add indexes on columns used in WHERE / JOIN clauses of the slow query."
            ))

    # RabbitMQ
    if _re.search(r'\brabbitmq\b|\bamqp\b', full):
        if _re.search(r'boot_failed|failed to start|rabbit.*crashed', full):
            findings.append((
                "💥",
                "RabbitMQ failed to boot. This is a startup failure — "
                "the broker could not initialize. "
                "Common causes: Erlang cookie mismatch, corrupted mnesia database, "
                "or port conflict with another RabbitMQ instance.",
                "Check if another RabbitMQ instance is running on the same ports. "
                "If the mnesia database is corrupted, delete the data volume and restart."
            ))
        if _re.search(r'disk_free_limit|low disk alarm|disk alarm', full):
            findings.append((
                "💾",
                "RabbitMQ triggered a disk alarm — available disk space dropped below "
                "its configured threshold. RabbitMQ blocks all producers when this happens "
                "to protect message durability.",
                "Free disk space on the host. "
                "You can also lower the `disk_free_limit` in the RabbitMQ config "
                "if disk space is intentionally limited."
            ))
        if _re.search(r'queue\.full|message.*rejected|channel.*flow|producer.*blocked|credit.*flow', full):
            findings.append((
                "📨",
                "RabbitMQ is applying flow control — a queue is full and producers are being blocked. "
                "Messages are piling up faster than consumers can process them. "
                "This causes SMS delivery delays and message rejections.",
                "Check queue depths in the RabbitMQ management UI (port 15672). "
                "Scale up the number of worker consumers, "
                "or increase the queue `max-length` if the backlog is temporary."
            ))
        if _re.search(r'consumer.*cancel|basic\.cancel|consumer.*tag.*removed|channel.*closed.*by.*server', full):
            findings.append((
                "👤",
                "A RabbitMQ consumer was cancelled or disconnected unexpectedly. "
                "The worker process lost its channel and stopped consuming messages. "
                "Messages in the queue will pile up until a consumer reconnects.",
                "Check the worker service logs for the reason it dropped the connection. "
                "Most clients auto-reconnect — if they do not, restart the worker service."
            ))
        if _re.search(r'message.*expired|ttl.*exceeded|dead.?letter', full):
            findings.append((
                "⏰",
                "Messages in RabbitMQ have expired (TTL exceeded) and were moved to the dead-letter queue. "
                "This means workers were too slow to process them within the allowed time window. "
                "In an SMS system this means delivery attempts were dropped.",
                "Check dead-letter queue contents in the management UI. "
                "Investigate why consumers are slow — resource bottleneck or code issue. "
                "Consider increasing the message TTL if the processing time is legitimately high."
            ))

    # JVM (Java/Kotlin/Scala services)
    if _re.search(r'heap space|stack overflow|java\.lang\.outofmemory|gc overhead|metaspace', full):
        findings.append((
            "🧱",
            "The JVM process ran out of memory. "
            "Either the heap space was exhausted (too many objects in memory), "
            "or the garbage collector spent more time collecting than executing code. "
            "The JVM killed the process.",
            f"Increase the JVM heap by adding `-Xmx512m` (or higher) to `JAVA_OPTS` "
            f"in the environment for `{service}` in docker-compose.yml."
        ))

    # SMPP / SMS workers
    if _re.search(r'\bsmpp\b|smsc|submit_sm|deliver_sm|enquire_link', full):
        if _re.search(r'smpp.*disconnect|smsc.*disconnect|connection.*lost|session.*closed|rebind|reconnect', full):
            findings.append((
                "📡",
                f"`{service}` lost its SMPP connection to the SMSC (SMS gateway). "
                "No SMS messages can be sent or received while disconnected. "
                "The worker should attempt to reconnect automatically.",
                "Check if the SMSC is reachable and the SMPP credentials are correct. "
                "Look for IP whitelist restrictions on the SMSC side. "
                "If the disconnect is frequent, check for keepalive (enquire_link) configuration."
            ))
        if _re.search(r'submit.*failed|delivery.*failed|dlr.*timeout|delivery receipt.*timeout|message.*undeliverable', full):
            findings.append((
                "📵",
                f"`{service}` is experiencing SMS delivery failures. "
                "Messages are being submitted to the SMSC but not delivered to handsets, "
                "or delivery receipts (DLRs) are not arriving within the expected window.",
                "Check the SMSC error codes in the logs — each code maps to a specific reason "
                "(e.g. invalid number, network error, subscriber absent). "
                "High DLR timeout rates may indicate SMSC-side congestion."
            ))
        if _re.search(r'throttl|rate.limit|too many submit|submit_sm.*error.*58|throughput.*exceeded', full):
            findings.append((
                "🚦",
                f"`{service}` is being throttled by the SMSC. "
                "The submission rate exceeds the allowed throughput on this SMPP connection. "
                "Messages are queued but delayed.",
                "Reduce the submission rate in the worker configuration (messages per second). "
                "Or negotiate a higher throughput limit with the SMSC provider. "
                "Adding more SMPP connections (binds) can also increase total throughput."
            ))

    # Catch-all: last line is "exit" with no other finding
    if not findings and lines and _re.match(r'^exit(ing)?$', lines[-1].lower()):
        findings.append((
            "⏹",
            f"`{service}` exited. The last log line was 'exit', meaning the main process ended "
            "and the container stopped. No error lines were found, so this may have been intentional.",
            f"If this was unintentional, restart: `docker compose up -d {service}`"
        ))

    # ── Build output ──────────────────────────────────────────────────────────
    sep   = "─" * 36
    parts = [f"🔍 *Log Analysis — {service}* (last {tail} lines)", sep]

    if not findings and not errors and not warnings:
        parts.append("*What happened:* ✅ The service is running normally — no errors or warnings found in these logs.")
        parts.append("")
        parts.append("*What to do:* Nothing. The service looks healthy.")
    else:
        # What happened
        parts.append("*What happened:*")
        if findings:
            for emoji, explanation, _ in findings:
                parts.append(f"  {emoji} {explanation}")
        elif errors:
            parts.append(f"  ❌ {len(errors)} error(s) were recorded in the logs.")
        if warnings and not findings:
            parts.append(f"  ⚠️  {len(warnings)} warning(s) are present but no critical errors.")

        # Key log lines
        parts.append("")
        if errors:
            parts.append("*Key log lines:*")
            seen, shown = set(), 0
            for e in errors:
                key = e[:90]
                if key not in seen and shown < 5:
                    parts.append(f"  `{e[:160]}`")
                    seen.add(key); shown += 1
        elif warnings:
            parts.append("*Key log lines:*")
            seen, shown = set(), 0
            for w in warnings:
                key = w[:90]
                if key not in seen and shown < 3:
                    parts.append(f"  `{w[:160]}`")
                    seen.add(key); shown += 1
        else:
            parts.append("*Recent log lines:*")
            for l in lines[-4:]:
                if l:
                    parts.append(f"  `{l[:160]}`")

        # What to do
        parts.append("")
        parts.append("*What to do:*")
        if findings:
            for emoji, _, advice in findings:
                parts.append(f"  {emoji} {advice}")
        elif errors:
            first_err = errors[0][:200] if errors else ""
            parts.append(
                f"  ❌ An error was recorded but it does not match a known pattern.\n"
                f"  Read the error message directly — it contains the real cause:\n"
                f"  `{first_err}`\n"
                f"  Search for the exact error text online or in the service's documentation."
            )
        elif warnings:
            first_warn = warnings[0][:200] if warnings else ""
            parts.append(
                f"  ⚠️  A warning is present but no critical error was found — the service is running.\n"
                f"  Warning: `{first_warn}`\n"
                f"  Warnings do not need immediate action but should be addressed before production."
            )

    parts.append(sep)
    return "\n".join(parts)


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
    Delete all messages in a Slack channel in parallel (10 concurrent workers).
    - User messages  → deleted with SLACK_USER_TOKEN (xoxp-)
    - Bot messages   → deleted with SLACK_BOT_TOKEN  (xoxb-)
    Skips system messages. Returns the number of messages deleted.
    """
    user_token = os.getenv("SLACK_USER_TOKEN", "")
    bot_token  = os.getenv("SLACK_BOT_TOKEN", "")
    if not user_token or not channel_id:
        return 0

    user_headers = {"Authorization": f"Bearer {user_token}"}
    bot_headers  = {"Authorization": f"Bearer {bot_token}"} if bot_token else user_headers

    # Step 1 — collect all messages in one pass (up to 200 per page)
    to_delete = []
    cursor = None
    while True:
        params = {"channel": channel_id, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _requests.get(
            "https://slack.com/api/conversations.history",
            headers=user_headers,
            params=params,
            timeout=10,
        ).json()
        for msg in data.get("messages", []):
            if msg.get("subtype") in ("channel_join", "channel_leave", "message_deleted"):
                continue
            is_bot = bool(msg.get("bot_id") or msg.get("subtype") == "bot_message")
            to_delete.append((msg["ts"], is_bot))
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not data.get("has_more") or not cursor:
            break

    if not to_delete:
        return 0

    # Step 2 — delete all in parallel
    def _delete(ts: str, is_bot: bool) -> bool:
        headers = bot_headers if is_bot else user_headers
        for _ in range(3):          # up to 3 retries on rate-limit
            r = _requests.post(
                "https://slack.com/api/chat.delete",
                headers=headers,
                json={"channel": channel_id, "ts": ts},
                timeout=10,
            ).json()
            if r.get("ok"):
                return True
            if r.get("error") == "ratelimited":
                time.sleep(float(r.get("headers", {}).get("Retry-After", 1)))
            else:
                return False
        return False

    deleted = 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_delete, ts, is_bot): ts for ts, is_bot in to_delete}
        for future in as_completed(futures):
            if future.result():
                deleted += 1

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
    import re as _re
    cmd = _re.sub(r'[?!.,;]+$', '', req.message.strip().lower()).strip()

    if cmd == "clear":
        clear_slack_history(req.channel_id)
        cm.reset()
        next_q = cm.current_question()
        combined = f"🧹 Chat cleared.\n\n{next_q}"
        return ChatResponse(session_id=req.session_id, reply="🧹 Chat cleared.",
                            next_question=combined, action_taken=False,
                            text=combined)

    # Friendly greeting — respond politely without showing the options menu
    _GREETINGS = {"hello", "hi", "hey", "bonjour", "salut", "yo", "good morning",
                  "good afternoon", "good evening", "greetings", "howdy"}
    if cmd in _GREETINGS:
        greet_reply = "Hello! 👋 I'm your Docker Agent. What would you like to do?\n(Type *help* to see all available options)"
        return ChatResponse(session_id=req.session_id, reply=greet_reply,
                            next_question=greet_reply, action_taken=False,
                            text=greet_reply)

    if cmd == "reset":
        cm.reset()
        next_q = cm.current_question()
        combined = f"🔄 Conversation reset.\n\n{next_q}"
        return ChatResponse(session_id=req.session_id, reply="🔄 Conversation reset.",
                            next_question=combined, action_taken=False,
                            text=combined)

    _HELP_TRIGGERS = {"help", "?", "what can you do", "what can i do", "options",
                      "commands", "menu", "capabilities", "what do you do"}
    if cmd in _HELP_TRIGGERS or any(t in cmd for t in ("what can", "what do you", "how can you", "show options")):
        help_text = (
            "Here's what I can do:\n"
            "─────────────────────────────────\n"
            "🐳 *add*  — add a new service to the stack\n"
            "🗑 *remove*  — remove a service from the stack\n"
            "📋 *list*  — show all services in docker-compose.yml\n"
            "📊 *status*  — show live container status\n"
            "🔍 *logs <service>*  — analyze container logs (e.g. logs redis)\n"
            "📈 *health*  — full infrastructure health report\n"
            "🔄 *reset*  — restart the conversation\n"
            "🧹 *clear*  — clear the Slack chat history\n"
            "─────────────────────────────────\n"
            "You can also speak naturally — e.g:\n"
            "  • \"give me the infra report\"\n"
            "  • \"show logs of nginx\"\n"
            "  • \"add a new service\""
        )
        next_q = "" if cm.step == Step.ASK_INTENT else cm.current_question()
        combined = f"{help_text}\n\n{next_q}".strip() if next_q else help_text
        return ChatResponse(session_id=req.session_id, reply=help_text,
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

    _STATUS_TRIGGERS = {"status", "containers", "ps", "show containers", "show status",
                        "container status", "how are my services", "show services"}
    if cmd in _STATUS_TRIGGERS or any(t in cmd for t in (
        "how are my container", "show me the container", "list container",
        "are my service", "service status",
    )):
        output = docker_manager.get_status()
        reply = _format_status(output)
        next_q = "" if cm.step == Step.ASK_INTENT else cm.current_question()
        combined = f"{reply}\n\n{next_q}".strip() if next_q else reply
        return ChatResponse(session_id=req.session_id, reply=reply,
                            next_question=combined, action_taken=False,
                            text=combined)

    # "logs nginx" / "logs of nginx" / "logs nginx 200" shorthand
    _cmd_parts = req.message.strip().split()
    if _cmd_parts and _cmd_parts[0].lower() == "logs" and len(_cmd_parts) >= 2:
        _PREP = {"of", "for", "from", "the", "on", "in", "a", "an"}
        _rest = _cmd_parts[1:]
        _svc_words  = [p for p in _rest if p.lower() not in _PREP and not p.isdigit()]
        _tail_words = [p for p in _rest if p.isdigit()]
        _svc  = _svc_words[0] if _svc_words else ""
        _tail = int(_tail_words[0]) if _tail_words else 100
        if _svc:
            reply = _run_log_analysis(_svc, _tail)
            next_q = "" if cm.step == Step.ASK_INTENT else cm.current_question()
            combined = f"{reply}\n\n{next_q}".strip() if next_q else reply
            return ChatResponse(session_id=req.session_id, reply=reply,
                                next_question=combined, action_taken=True,
                                text=combined)

    _HEALTH_TRIGGERS = {
        "health", "health report", "daily report", "infrastructure report", "report",
        "how is my infrastructure", "how is my infra", "infrastructure status",
        "system report", "system health", "give me a report", "show report",
        "send me the report", "send report", "morning report", "daily digest",
        "infra report", "cluster health", "show health", "give me report",
        "give me the report", "show me the report", "give me the infra report",
        "give me the infrastructure report", "show me the infra report",
        "how is the infra", "how is the infrastructure", "infra status",
    }
    if cmd in _HEALTH_TRIGGERS or any(t in cmd for t in (
        "health report", "daily report", "infrastructure", "how is my infra",
        "system report", "morning report", "give me a report", "show me a report",
        "give me report", "give me the report", "infra report", "show the report",
        "how is the infra", "report of my infra", "report of the infra",
        "how is my infra", "infra", "report",
    )):
        result = health_report()
        report_text = result["text"]
        next_q = "" if cm.step == Step.ASK_INTENT else cm.current_question()
        combined = f"{report_text}\n\n{next_q}".strip() if next_q else report_text
        return ChatResponse(session_id=req.session_id, reply=report_text,
                            next_question=combined, action_taken=False,
                            text=combined)

    if cmd in ("quit", "exit", "bye", "goodbye", "see you", "later", "cya"):
        cm.reset()
        goodbye = "👋 Goodbye! See you next time. Come back whenever you need me — type *help* to see what I can do."
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
        if step == Step.ASK_INTENT:
            # Unrecognized intent — just show the hint, no greeting below it
            return ChatResponse(
                session_id=req.session_id,
                reply=error,
                next_question=error,
                action_taken=False,
                text=error,
            )
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

    if step == Step.READY_TO_ANALYZE:
        reply = _run_log_analysis(cm.log_request.service, cm.log_request.tail)
        action_taken = True
        cm.reset()
        suppress_next_q = True

    elif step == Step.READY_TO_ADD:
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


class AnalyzeLogsRequest(BaseModel):
    service: str
    tail: int = 100


@app.post("/analyze-logs")
def analyze_logs(req: AnalyzeLogsRequest):
    """
    Direct endpoint to analyze logs for a service.
    Can be called from n8n or curl without going through the chat flow.
    """
    result = _run_log_analysis(req.service, req.tail)
    return {"service": req.service, "tail": req.tail, "text": result}


@app.get("/health-report")
def health_report():
    """
    Generate a full infrastructure health report.
    Called by n8n cron at 8AM or on demand via the chat command 'health'.
    Uses a fast rule-based summary (no LLM) to keep response time under 2s.
    """
    data = health_reporter.collect()
    llm_summary = _fast_health_summary(data)
    report = report_formatter.build_slack_report(data, llm_summary)
    return {"report": report, "text": report}


def _fast_health_summary(data: dict) -> str:
    """Generate an instant health summary without calling the LLM."""
    running  = data["running"]
    total    = data["total"]
    stopped  = total - running
    critical = data["critical"]
    warnings = data["warnings"]

    parts = []

    if critical == 0 and warnings == 0 and stopped == 0:
        parts.append(f"All {total} services are running normally — no issues detected.")
    else:
        parts.append(f"{running}/{total} services are running.")
        if stopped:
            stopped_names = [c["name"] for c in data["containers"] if c["status"] != "running"]
            parts.append(f"{stopped} service(s) are down: {', '.join(stopped_names)}.")
        if critical:
            parts.append(f"{critical} critical alert(s) require immediate attention.")
        if warnings:
            parts.append(f"{warnings} warning(s) are active.")

    if not parts:
        parts.append("Infrastructure status collected.")

    return " ".join(parts)


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
