"""
report_formatter.py — Build the Slack health report digest.

Format matches the reference design:
  🌅 Good morning — Daily Infrastructure Report
  📅 Friday, March 28 2026 — 08:00

  ✅ Services running: 7/7
  ⚠️  1 warning: nginx memory above 80%
  🟢 0 critical alerts

  📊 Top resource consumers:
    • postgres → CPU 12%, RAM 340MB

  🕐 Uptime: all services stable for 18h+

  💡 AI Summary:
  Your infrastructure is healthy...

  📈 Full dashboard → http://localhost:3000
"""

import os
from datetime import datetime, timezone


def _top_consumers(metrics: dict, n: int = 3) -> list[dict]:
    ranked = []
    for name, m in metrics.items():
        if name.startswith("/"):
            name = name[1:]
        ranked.append({
            "name":    name,
            "cpu_pct": m.get("cpu_pct", 0.0),
            "mem_mb":  m.get("mem_mb", 0.0),
        })
    ranked.sort(key=lambda x: x["cpu_pct"], reverse=True)
    return ranked[:n]


def _compute_uptime(containers: list[dict]) -> str:
    """
    Return uptime summary based on running containers.
    - Excludes 'docker-agent' (gets rebuilt frequently, skews the number).
    - Ignores containers restarted in the last 5 min (intentional restarts).
    - Uses the minimum uptime of the remaining services.
    - If some containers were recently restarted, reports 'X/Y services stable for Zh+'.
    """
    now = datetime.now(timezone.utc).astimezone()
    uptimes = []
    skipped = 0

    for c in containers:
        if c["status"] != "running" or not c.get("started"):
            continue
        if c["name"] in ("docker-agent",):   # always exclude the agent itself
            continue
        try:
            started_str = c["started"].replace(" ", "T")
            started = datetime.fromisoformat(started_str)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            secs = int((now - started).total_seconds())
            if secs < 300:           # restarted < 5 min ago — skip (likely intentional)
                skipped += 1
            else:
                uptimes.append(secs)
        except Exception:
            pass

    if not uptimes:
        return ""

    min_secs = min(uptimes)
    stable   = len(uptimes)
    total    = stable + skipped

    def _fmt(s: int) -> str:
        if s >= 86400:
            return f"{s // 86400}d+"
        if s >= 3600:
            return f"{s // 3600}h+"
        return f"{s // 60}m+"

    prefix = f"{stable}/{total} services" if skipped else "all services"
    return f"{prefix} stable for {_fmt(min_secs)}"


def build_llm_input(data: dict) -> str:
    """Structured text block for the AI summary (kept for compatibility)."""
    now = datetime.now(timezone.utc).astimezone().strftime("%A, %B %d %Y — %H:%M")
    lines = [
        f"Date: {now}",
        f"Services running: {data['running']}/{data['total']}",
        f"Active warnings: {data['warnings']}",
        f"Active critical alerts: {data['critical']}",
        "",
        "Container list:",
    ]
    for c in data["containers"]:
        m = data["metrics"].get(c["name"], data["metrics"].get("/" + c["name"], {}))
        cpu = f"CPU {m['cpu_pct']}%" if "cpu_pct" in m else ""
        mem = f"RAM {m['mem_mb']}MB"  if "mem_mb"  in m else ""
        perf = f"  [{', '.join(filter(None, [cpu, mem]))}]" if (cpu or mem) else ""
        lines.append(f"  - {c['name']}: {c['status']}{perf}")
    if data["alerts"]:
        lines.append("")
        lines.append("Active alerts:")
        for a in data["alerts"]:
            lines.append(f"  - [{a['severity'].upper()}] {a['alertname']}: {a['summary']}")
    return "\n".join(lines)


def build_slack_report(data: dict, llm_summary: str) -> str:
    """Build the Slack digest matching the reference design."""
    local_dt  = datetime.now(timezone.utc).astimezone()
    hour      = local_dt.hour
    greeting  = "Good morning" if hour < 12 else ("Good afternoon" if hour < 18 else "Good evening")
    now_str   = local_dt.strftime("%A, %B %d %Y — %H:%M")

    running  = data["running"]
    total    = data["total"]
    critical = data["critical"]
    alerts   = data["alerts"]

    warning_alerts  = [a for a in alerts if a.get("severity") == "warning"]
    critical_alerts = [a for a in alerts if a.get("severity") == "critical"]

    lines = [
        f"🌅 *{greeting} — Daily Infrastructure Report*",
        f"📅 _{now_str}_",
        "",
    ]

    # ── Services running ─────────────────────────────────────────────────────
    svc_icon = "✅" if running == total else "⚠️"
    lines.append(f"{svc_icon} *Services running:* {running}/{total}")

    # ── Warnings (show actual alert description) ──────────────────────────────
    if warning_alerts:
        first = warning_alerts[0]
        desc  = first.get("summary") or first.get("alertname", "warning")
        extra = f" (+{len(warning_alerts) - 1} more)" if len(warning_alerts) > 1 else ""
        lines.append(f"⚠️  *{len(warning_alerts)} warning:* {desc}{extra}")
    else:
        lines.append("✅ *No warnings*")

    # ── Critical alerts ───────────────────────────────────────────────────────
    if critical_alerts:
        descs = [a.get("summary") or a.get("alertname", "critical") for a in critical_alerts]
        lines.append(f"🔴 *{len(critical_alerts)} critical:*")
        for desc in descs:
            lines.append(f"  • {desc}")
    else:
        lines.append("🟢 *0 critical alerts*")

    # ── Top resource consumers ────────────────────────────────────────────────
    top = _top_consumers(data["metrics"])
    if top:
        lines.append("")
        lines.append("📊 *Top resource consumers:*")
        for t in top:
            cpu_str = f"CPU {t['cpu_pct']}%" if t["cpu_pct"] else "CPU n/a"
            mem_str = f"RAM {t['mem_mb']}MB"  if t["mem_mb"]  else "RAM n/a"
            lines.append(f"  • {t['name']} → {cpu_str}, {mem_str}")

    # ── Queue depths (RabbitMQ) ───────────────────────────────────────────────
    queue_depths = data.get("queue_depths", [])
    if queue_depths:
        busy = [q for q in queue_depths if q["messages"] > 0]
        if busy:
            lines.append("")
            lines.append("📨 *RabbitMQ queues:*")
            for q in busy[:3]:
                consumers = q["consumers"]
                consumer_str = f"{consumers} consumer{'s' if consumers != 1 else ''}"
                status = "⚠️" if q["messages"] > 1000 else "🟡" if q["messages"] > 100 else "🟢"
                lines.append(f"  {status} {q['queue']}: {q['messages']} msgs, {consumer_str}")

    # ── Redis memory ──────────────────────────────────────────────────────────
    redis_memory = data.get("redis_memory", {})
    if redis_memory.get("used_mb"):
        lines.append("")
        if redis_memory.get("used_pct") is not None:
            pct = redis_memory["used_pct"]
            icon = "🔴" if pct > 90 else "🟡" if pct > 75 else "🟢"
            lines.append(f"🧠 *Redis memory:* {icon} {redis_memory['used_mb']}MB / {redis_memory['max_mb']}MB ({pct}%)")
        else:
            lines.append(f"🧠 *Redis memory:* {redis_memory['used_mb']}MB used")

    # ── Uptime ────────────────────────────────────────────────────────────────
    uptime = _compute_uptime(data["containers"])
    if uptime:
        lines.append("")
        lines.append(f"🕐 *Uptime:* {uptime}")

    # ── AI Summary ────────────────────────────────────────────────────────────
    lines.append("")
    lines.append("💡 *AI Summary:*")
    lines.append(llm_summary)

    # ── Grafana link ──────────────────────────────────────────────────────────
    grafana_url = os.getenv("GRAFANA_URL", "http://localhost:3000")
    lines.append("")
    lines.append(f"📈 *Full dashboard →* {grafana_url}")

    return "\n".join(lines)
