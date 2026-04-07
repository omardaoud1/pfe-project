"""
health_reporter.py — Collect infrastructure health data.

Fetches:
  - Container status (up/down, uptime) via Docker SDK
  - CPU & RAM per container from Prometheus (cadvisor) if available
  - Active firing alerts from Prometheus

Returns a raw dict that report_formatter.py structures for the LLM.
"""

import docker
import yaml
import prometheus_client as prom

# docker-compose.yml is mounted at this path inside the container
_COMPOSE_PATH = "/monitoring/docker-compose.yml"


def _get_compose_service_names() -> set[str]:
    """
    Read service names from the mounted docker-compose.yml.
    Container names default to the service name (or container_name if set).
    """
    try:
        with open(_COMPOSE_PATH) as f:
            compose = yaml.safe_load(f)
        names = set()
        for svc_key, svc_val in (compose.get("services") or {}).items():
            # prefer explicit container_name, else fall back to service key
            names.add((svc_val or {}).get("container_name", svc_key))
        return names
    except Exception:
        return set()


def _get_docker_status() -> list[dict]:
    """
    Return list of {name, status, uptime} for containers defined in
    docker-compose.yml (filtered via the mounted compose file).
    """
    try:
        compose_names = _get_compose_service_names()
        client = docker.from_env()
        containers = client.containers.list(all=True)
        result = []
        for c in containers:
            if compose_names and c.name not in compose_names:
                continue
            c.reload()
            state = c.attrs.get("State", {})
            status = "running" if state.get("Running") else "stopped"
            started = state.get("StartedAt", "")[:19].replace("T", " ")
            result.append({
                "name":    c.name,
                "status":  status,
                "started": started,
            })
        # preserve compose order where possible
        if compose_names:
            order = list(compose_names)
            result.sort(key=lambda x: order.index(x["name"]) if x["name"] in order else 999)
        return result
    except Exception as e:
        return [{"name": "docker", "status": f"error: {e}", "started": ""}]


def collect() -> dict:
    """
    Collect all health data and return a raw dict:
    {
        "containers": [...],
        "metrics":    {container_name: {cpu_pct, mem_mb}},
        "alerts":     [...],
        "running":    int,
        "total":      int,
        "warnings":   int,
        "critical":   int,
    }
    """
    containers   = _get_docker_status()
    metrics      = prom.get_all_container_metrics()
    alerts       = prom.get_active_alerts()
    queue_depths = prom.get_rabbitmq_queue_depths()
    redis_memory = prom.get_redis_memory_usage()

    running  = sum(1 for c in containers if c["status"] == "running")
    total    = len(containers)
    warnings = sum(1 for a in alerts if a.get("severity") == "warning")
    critical = sum(1 for a in alerts if a.get("severity") == "critical")

    return {
        "containers":   containers,
        "metrics":      metrics,
        "alerts":       alerts,
        "running":      running,
        "total":        total,
        "warnings":     warnings,
        "critical":     critical,
        "queue_depths": queue_depths,
        "redis_memory": redis_memory,
    }
