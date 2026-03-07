"""
docker-watcher — Auto-discovery service
=======================================
Watches the monitoring_default Docker network every 30 seconds.
If a NEW container appears that has no rule → adds it to:
  1. decision-engine/app/rules.py
  2. action-executor/main.py            (ACTION_MAP)
  3. prometheus/prometheus.yml          (blackbox probe target)
  4. prometheus/rules/auto-discovered.yml  (alert rule)

Then reloads Prometheus via its HTTP API so changes take effect immediately.

Container label convention (add to any new service in docker-compose):
  labels:
    - "monitoring.port=8080"    ← required to enable Prometheus probe
    - "monitoring.probe=http"   ← optional: "http" or "tcp" (default: tcp)

Removal is intentionally NOT handled.
Stopping a container for testing must never delete its rule.
"""

import time
import re
import os
import requests
import docker

# ── Paths (mounted as volumes in docker-compose) ──────────────────────────────
RULES_PATH       = "/rules/rules.py"
ACTION_MAP_PATH  = "/action/main.py"
PROMETHEUS_YML   = "/prometheus/prometheus.yml"
AUTO_ALERTS_PATH = "/prometheus/rules/auto-discovered.yml"

# ── Prometheus reload endpoint ────────────────────────────────────────────────
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")

# ── Services the watcher should NEVER touch ───────────────────────────────────
IGNORED_CONTAINERS = {
    "prometheus",
    "alertmanager",
    "grafana",
    "node_exporter",
    "redis_exporter",
    "redis_history_exporter",
    "rabbitmq_exporter",
    "blackbox_exporter",
    "n8n",
    "decision-engine",
    "action-executor",
    "docker-watcher",
}

# ── Services already covered by hand-written rules ────────────────────────────
KNOWN_SERVICES = {
    "redis",
    "redis-history",
    "rabbitmq",
    "gateway",
}


# ══════════════════════════════════════════════════════════════════════════════
# Docker helpers
# ══════════════════════════════════════════════════════════════════════════════

def get_network_containers(client) -> list:
    """
    Returns container objects in monitoring_default network only.
    Scoped to this network so unrelated host containers are never picked up.
    """
    containers = []
    try:
        network = client.networks.get("monitoring_default")
        network.reload()
        for container_id in network.attrs.get("Containers", {}).keys():
            try:
                containers.append(client.containers.get(container_id))
            except docker.errors.NotFound:
                pass
    except docker.errors.NotFound:
        print("[WATCHER] ⚠️  monitoring_default network not found yet — retrying next cycle")
    return containers


# ══════════════════════════════════════════════════════════════════════════════
# rules.py
# ══════════════════════════════════════════════════════════════════════════════

def get_known_services_from_rules(content: str) -> set:
    return set(re.findall(r'service\s*==\s*"([^"]+)"', content))


def build_rule_block(service_name: str, action_key: str) -> str:
    incident_type = f"{service_name.replace('-', '').replace('_', '').title()}Down"
    return f"""
    # --------------------
    # {service_name} down (auto-discovered)
    # --------------------
    if incident_type == "{incident_type}" and service == "{service_name}":
        return RuleResult(
            action="{action_key}",
            action_params={{}},
            base_confidence=0.3,
            safety_level=2,
            reason="Auto-discovered service — low confidence baseline"
        )
"""


def add_rule_to_rules_py(service_name: str, action_key: str):
    with open(RULES_PATH, "r") as f:
        content = f.read()

    fallback_marker = "    # --------------------\n    # Fallback (unknown incident)"
    new_block = build_rule_block(service_name, action_key)

    if fallback_marker not in content:
        print(f"[WATCHER] ⚠️  Fallback marker not found — appending at end of rules.py")
        new_content = content + new_block
    else:
        new_content = content.replace(fallback_marker, new_block + fallback_marker)

    with open(RULES_PATH, "w") as f:
        f.write(new_content)

    print(f"[WATCHER] ✅ rules.py — added rule for '{service_name}'")


# ══════════════════════════════════════════════════════════════════════════════
# main.py (ACTION_MAP)
# ══════════════════════════════════════════════════════════════════════════════

def get_action_key(service_name: str) -> str:
    return f"restart_{service_name.replace('-', '_')}"


def get_known_actions_from_action_map(content: str) -> set:
    return set(re.findall(r'"(restart_[^"]+)"\s*:', content))


def add_action_to_action_map(action_key: str, service_name: str):
    with open(ACTION_MAP_PATH, "r") as f:
        content = f.read()

    action_map_start = content.find('ACTION_MAP')
    action_map_end   = content.find('\n}\n', action_map_start)
    if action_map_end == -1:
        print(f"[WATCHER] ⚠️  ACTION_MAP closing brace not found — skipping")
        return

    # Ensure trailing comma on the last existing entry
    before_closing = content[:action_map_end]
    last_nl        = before_closing.rfind('\n')
    stripped       = before_closing[last_nl + 1:].rstrip()

    if stripped and not stripped.endswith(','):
        insert_pos = last_nl + 1 + len(stripped)
        content    = content[:insert_pos] + ',' + content[insert_pos:]
        action_map_end = content.find('\n}\n', action_map_start)

    new_entry   = f'    "{action_key}":         ["docker", "restart", "{service_name}"],\n'
    new_content = content[:action_map_end] + "\n" + new_entry.rstrip("\n") + content[action_map_end:]

    with open(ACTION_MAP_PATH, "w") as f:
        f.write(new_content)

    print(f"[WATCHER] ✅ main.py — added '{action_key}' to ACTION_MAP")


# ══════════════════════════════════════════════════════════════════════════════
# Prometheus — prometheus.yml
# ══════════════════════════════════════════════════════════════════════════════

def build_prometheus_job(service_name: str, port: str, probe: str) -> str:
    """
    Blackbox scrape job.
    Uses http_2xx module for http probe, tcp_connect for tcp probe.
    blackbox.yml already has http_2xx — tcp_connect needs to be added there too.
    """
    module = "http_2xx" if probe == "http" else "tcp_connect"
    return f"""
  # auto-discovered: {service_name}
  - job_name: "blackbox_{service_name}"
    metrics_path: /probe
    params:
      module: [{module}]
    static_configs:
      - targets:
        - {service_name}:{port}
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - target_label: __address__
        replacement: blackbox_exporter:9115
      - source_labels: [__param_target]
        target_label: instance
"""


def add_prometheus_job(service_name: str, port: str, probe: str):
    with open(PROMETHEUS_YML, "r") as f:
        content = f.read()

    new_content = content.rstrip() + "\n" + build_prometheus_job(service_name, port, probe)

    with open(PROMETHEUS_YML, "w") as f:
        f.write(new_content)

    print(f"[WATCHER] ✅ prometheus.yml — added blackbox_{service_name} job ({probe}:{port})")


def is_job_in_prometheus(service_name: str) -> bool:
    try:
        with open(PROMETHEUS_YML, "r") as f:
            return f'job_name: "blackbox_{service_name}"' in f.read()
    except FileNotFoundError:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Prometheus — auto-discovered.yml alert rules
# ══════════════════════════════════════════════════════════════════════════════

def ensure_auto_alerts_file():
    if not os.path.exists(AUTO_ALERTS_PATH):
        with open(AUTO_ALERTS_PATH, "w") as f:
            f.write("groups:\n- name: auto-discovered-alerts\n  rules:\n")
        print(f"[WATCHER] ✅ Created {AUTO_ALERTS_PATH}")


def build_alert_rule(service_name: str) -> str:
    """
    Alert fires when blackbox probe fails for 10s.
    incident_type naming matches what decision-engine expects in rules.py.
    """
    incident_type = f"{service_name.replace('-', '').replace('_', '').title()}Down"
    return f"""
  - alert: {incident_type}
    expr: probe_success{{job="blackbox_{service_name}"}} == 0
    for: 10s
    labels:
      severity: critical
      service: {service_name}
    annotations:
      summary: "{service_name} is down"
      description: "Auto-discovered service {service_name} probe failed"
"""


def add_alert_rule(service_name: str):
    ensure_auto_alerts_file()

    with open(AUTO_ALERTS_PATH, "r") as f:
        content = f.read()

    new_content = content.rstrip() + "\n" + build_alert_rule(service_name)

    with open(AUTO_ALERTS_PATH, "w") as f:
        f.write(new_content)

    print(f"[WATCHER] ✅ auto-discovered.yml — added alert for '{service_name}'")


def is_alert_in_file(service_name: str) -> bool:
    try:
        incident_type = f"{service_name.replace('-', '').replace('_', '').title()}Down"
        with open(AUTO_ALERTS_PATH, "r") as f:
            return f"alert: {incident_type}" in f.read()
    except FileNotFoundError:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Prometheus reload
# ══════════════════════════════════════════════════════════════════════════════

def reload_prometheus():
    """
    POST /-/reload triggers Prometheus to re-read prometheus.yml and rules/.
    Requires Prometheus to be started with --web.enable-lifecycle.
    """
    try:
        resp = requests.post(f"{PROMETHEUS_URL}/-/reload", timeout=5)
        if resp.status_code == 200:
            print(f"[WATCHER] ✅ Prometheus reloaded")
        else:
            print(f"[WATCHER] ⚠️  Prometheus reload returned HTTP {resp.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"[WATCHER] ⚠️  Prometheus reload failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Main processing
# ══════════════════════════════════════════════════════════════════════════════

def process_new_service(container):
    """
    Full registration pipeline for a newly discovered service.
    """
    name       = container.name.lstrip("/")
    action_key = get_action_key(name)
    labels     = container.labels or {}
    port       = labels.get("monitoring.port", None)
    probe      = labels.get("monitoring.probe", "tcp").lower()

    print(f"[WATCHER] 🆕 New service: '{name}' | port={port or 'not set'} | probe={probe}")

    # 1. rules.py
    try:
        add_rule_to_rules_py(name, action_key)
    except Exception as e:
        print(f"[WATCHER] ❌ rules.py failed for '{name}': {e}")
        return

    # 2. ACTION_MAP
    try:
        add_action_to_action_map(action_key, name)
    except Exception as e:
        print(f"[WATCHER] ❌ main.py failed for '{name}': {e}")
        return

    # 3 & 4. Prometheus (only if monitoring.port label is present)
    prometheus_updated = False
    if port:
        try:
            add_prometheus_job(name, port, probe)
            prometheus_updated = True
        except Exception as e:
            print(f"[WATCHER] ❌ prometheus.yml failed for '{name}': {e}")

        try:
            add_alert_rule(name)
        except Exception as e:
            print(f"[WATCHER] ❌ auto-discovered.yml failed for '{name}': {e}")

        # 5. Reload Prometheus so changes are live immediately
        if prometheus_updated:
            reload_prometheus()
    else:
        print(
            f"[WATCHER] ⚠️  '{name}' has no monitoring.port label — "
            f"registered in rules.py and main.py but Prometheus probe skipped.\n"
            f"           Add label 'monitoring.port=PORT' to docker-compose to enable full monitoring."
        )

    print(f"[WATCHER] ✅ '{name}' registration complete")


def watch():
    print("[WATCHER] 🚀 Starting docker-watcher...")
    print(f"[WATCHER] rules.py        → {RULES_PATH}")
    print(f"[WATCHER] main.py         → {ACTION_MAP_PATH}")
    print(f"[WATCHER] prometheus.yml  → {PROMETHEUS_YML}")
    print(f"[WATCHER] auto-alerts     → {AUTO_ALERTS_PATH}")

    client            = docker.from_env()
    already_processed = set()

    while True:
        try:
            with open(RULES_PATH, "r") as f:
                rules_content = f.read()
            with open(ACTION_MAP_PATH, "r") as f:
                action_content = f.read()

            known_in_rules   = get_known_services_from_rules(rules_content)
            known_in_actions = get_known_actions_from_action_map(action_content)
            containers       = get_network_containers(client)

            for container in containers:
                name = container.name.lstrip("/")

                if name in IGNORED_CONTAINERS:
                    continue
                if name in KNOWN_SERVICES:
                    continue
                if name in already_processed:
                    continue
                if name in known_in_rules:
                    already_processed.add(name)
                    continue
                if get_action_key(name) in known_in_actions:
                    already_processed.add(name)
                    continue

                process_new_service(container)
                already_processed.add(name)

        except docker.errors.DockerException as e:
            print(f"[WATCHER] ❌ Docker error: {e}")
        except FileNotFoundError as e:
            print(f"[WATCHER] ❌ File not found: {e}")
        except Exception as e:
            print(f"[WATCHER] ❌ Unexpected error: {e}")

        time.sleep(30)


if __name__ == "__main__":
    watch()
