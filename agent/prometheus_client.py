"""
prometheus_client.py — Reusable Prometheus HTTP API wrapper.

Queries Prometheus at /api/v1/query and /api/v1/query_range.
Used by health_reporter.py for CPU, RAM, uptime metrics.

Prometheus URL is read from the PROMETHEUS_URL env var,
defaulting to http://prometheus:9090 (inside Docker network).
"""

import os
import requests

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")


def query(promql: str) -> list[dict]:
    """
    Run an instant PromQL query.
    Returns the list of result dicts: [{"metric": {...}, "value": [ts, "val"]}, ...]
    Returns [] on error.
    """
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": promql},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            return data["data"]["result"]
    except Exception:
        pass
    return []


def query_single(promql: str, default=None):
    """
    Run a PromQL query and return the first scalar value as a float.
    Returns default if no result or on error.
    """
    results = query(promql)
    if results:
        try:
            return float(results[0]["value"][1])
        except (KeyError, IndexError, ValueError):
            pass
    return default


def get_cpu_percent(container: str) -> float | None:
    """
    Return CPU usage % for a container (last 1 minute average).
    Uses cadvisor metrics if available, otherwise returns None.
    """
    promql = (
        f'rate(container_cpu_usage_seconds_total{{name="{container}"}}[1m]) * 100'
    )
    return query_single(promql)


def get_memory_mb(container: str) -> float | None:
    """Return resident memory in MB for a container."""
    promql = f'container_memory_rss{{name="{container}"}} / 1024 / 1024'
    return query_single(promql)


def get_all_container_metrics() -> dict[str, dict]:
    """
    Return a dict of {container_name: {cpu_pct, mem_mb}} for all containers
    that have cadvisor metrics. Falls back gracefully if cadvisor is absent.
    """
    metrics: dict[str, dict] = {}

    cpu_results = query('rate(container_cpu_usage_seconds_total[1m]) * 100')
    for r in cpu_results:
        name = r.get("metric", {}).get("name", "")
        if not name:
            continue
        try:
            val = float(r["value"][1])
        except (KeyError, IndexError, ValueError):
            val = 0.0
        metrics.setdefault(name, {})["cpu_pct"] = round(val, 2)

    mem_results = query('container_memory_rss / 1024 / 1024')
    for r in mem_results:
        name = r.get("metric", {}).get("name", "")
        if not name:
            continue
        try:
            val = float(r["value"][1])
        except (KeyError, IndexError, ValueError):
            val = 0.0
        metrics.setdefault(name, {})["mem_mb"] = round(val, 1)

    return metrics


def get_active_alerts() -> list[dict]:
    """
    Return list of currently firing alerts from Prometheus.
    Each dict has: alertname, severity, labels, summary.
    """
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/alerts",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        alerts = []
        for a in data.get("data", {}).get("alerts", []):
            if a.get("state") == "firing":
                alerts.append({
                    "alertname": a["labels"].get("alertname", "unknown"),
                    "severity":  a["labels"].get("severity", "info"),
                    "labels":    a.get("labels", {}),
                    "summary":   a.get("annotations", {}).get("summary", ""),
                })
        return alerts
    except Exception:
        return []


def get_rabbitmq_queue_depths() -> list[dict]:
    """
    Return list of {queue, messages, consumers} for RabbitMQ queues,
    sorted by message count descending. Top 5 only.
    Uses kbudde/rabbitmq-exporter metrics.
    """
    msg_results = query('rabbitmq_queue_messages')
    if not msg_results:
        return []

    consumer_results = query('rabbitmq_queue_consumers')
    consumer_map: dict[str, int] = {}
    for r in consumer_results:
        name = r.get("metric", {}).get("queue", "")
        if name:
            try:
                consumer_map[name] = int(float(r["value"][1]))
            except (KeyError, IndexError, ValueError):
                pass

    queues = []
    for r in msg_results:
        name = r.get("metric", {}).get("queue", "")
        if not name:
            continue
        try:
            messages = int(float(r["value"][1]))
        except (KeyError, IndexError, ValueError):
            messages = 0
        queues.append({
            "queue":     name,
            "messages":  messages,
            "consumers": consumer_map.get(name, 0),
        })

    queues.sort(key=lambda x: x["messages"], reverse=True)
    return queues[:5]


def get_redis_memory_usage() -> dict:
    """
    Return {used_mb, max_mb, used_pct} for the main Redis instance.
    Uses oliver006/redis_exporter metrics.
    Returns empty dict if exporter is unavailable.
    """
    used_results = query('redis_memory_used_bytes')
    max_results  = query('redis_maxmemory_bytes')

    if not used_results:
        return {}

    try:
        used_bytes = float(used_results[0]["value"][1])
    except (KeyError, IndexError, ValueError):
        return {}

    result = {"used_mb": round(used_bytes / 1024 / 1024, 1)}

    if max_results:
        try:
            max_bytes = float(max_results[0]["value"][1])
            if max_bytes > 0:
                result["max_mb"]   = round(max_bytes / 1024 / 1024, 1)
                result["used_pct"] = round(used_bytes / max_bytes * 100, 1)
        except (KeyError, IndexError, ValueError):
            pass

    return result
