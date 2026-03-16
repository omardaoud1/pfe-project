"""
cleanup_manager.py — Removes a service from ALL files (REMOVE flow).

When the user removes a service, docker-watcher only handles ADD.
Removal must be done manually across these files:
  1. docker-compose.yml        — remove the service block
  2. decision-engine/app/rules.py   — remove the if-block for this service
  3. action-executor/main.py        — remove the ACTION_MAP entry
  4. prometheus/prometheus.yml      — remove the blackbox scrape job
  5. prometheus/rules/auto-discovered.yml — remove the alert rule

After cleanup:
  - docker compose stop + rm <service>
  - Prometheus reload (POST /-/reload)
"""

import os
import re
import subprocess
import yaml
import requests

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

COMPOSE_FILE     = os.path.join(BASE, "monitoring", "docker-compose.yml")
COMPOSE_DIR      = os.path.join(BASE, "monitoring")
RULES_FILE       = os.path.join(BASE, "decision-engine", "app", "rules.py")
ACTION_FILE      = os.path.join(BASE, "action-executor", "main.py")
PROMETHEUS_YML   = os.path.join(BASE, "monitoring", "prometheus", "prometheus.yml")
AUTO_DISC_FILE   = os.path.join(BASE, "monitoring", "prometheus", "rules", "auto-discovered.yml")
PROMETHEUS_URL   = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")


# ---------------------------------------------------------------------------
# 1. docker-compose.yml
# ---------------------------------------------------------------------------

def remove_from_compose(service_name: str) -> bool:
    """
    Remove a service block from docker-compose.yml.
    Returns True if the service was found and removed.
    """
    with open(COMPOSE_FILE, "r") as f:
        data = yaml.safe_load(f)

    services = data.get("services", {})
    if service_name not in services:
        return False

    del services[service_name]
    data["services"] = services

    with open(COMPOSE_FILE, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return True


# ---------------------------------------------------------------------------
# 2. decision-engine/app/rules.py
# ---------------------------------------------------------------------------

def remove_from_rules(service_name: str) -> bool:
    """
    Remove the auto-generated if-block for this service from rules.py.

    docker-watcher writes blocks in this exact format:
        \n    # --------------------
        \n    # <service_name> down (auto-discovered)
        \n    # --------------------
        \n    if incident_type == "..." and service == "<service_name>":
        \n        return RuleResult(
        \n            ...
        \n        )
    """
    with open(RULES_FILE, "r") as f:
        content = f.read()

    # Match the comment header + if-block up to and including the closing )
    pattern = (
        rf'\n\s+# --------------------\n'
        rf'\s+# {re.escape(service_name)} down \(auto-discovered\)\n'
        rf'\s+# --------------------\n'
        rf'\s+if incident_type.*?and service == "{re.escape(service_name)}".*?'
        rf'\n\s+\)\n'
    )
    new_content, count = re.subn(pattern, "\n", content, flags=re.DOTALL)

    if count == 0:
        return False

    with open(RULES_FILE, "w") as f:
        f.write(new_content)

    return True


# ---------------------------------------------------------------------------
# 3. action-executor/main.py
# ---------------------------------------------------------------------------

def remove_from_action_map(service_name: str) -> bool:
    """
    Remove the ACTION_MAP entry for this service from action-executor/main.py.

    docker-watcher writes entries like:
        "restart_<service_name_underscored>": ["docker", "restart", "<service_name>"],

    We match the line and remove it.
    Returns True if an entry was found and removed.
    """
    action_key = "restart_" + service_name.replace("-", "_")

    with open(ACTION_FILE, "r") as f:
        lines = f.readlines()

    new_lines = [l for l in lines if f'"{action_key}"' not in l]

    if len(new_lines) == len(lines):
        return False  # nothing removed

    with open(ACTION_FILE, "w") as f:
        f.writelines(new_lines)

    return True


# ---------------------------------------------------------------------------
# 4. prometheus/prometheus.yml
# ---------------------------------------------------------------------------

def remove_from_prometheus(service_name: str) -> bool:
    """
    Remove the blackbox scrape job for this service from prometheus.yml.

    docker-watcher writes a block like:
      # auto-discovered: <service_name>
      - job_name: "blackbox_<service_name>"
        ...

    We remove from the comment line to the next top-level job or EOF.
    """
    with open(PROMETHEUS_YML, "r") as f:
        content = f.read()

    pattern = (
        rf'\n  # auto-discovered: {re.escape(service_name)}\n'
        rf'  - job_name: "blackbox_{re.escape(service_name)}".*?'
        rf'(?=\n  # auto-discovered:|\n  - job_name:|\Z)'
    )
    new_content, count = re.subn(pattern, "", content, flags=re.DOTALL)

    if count == 0:
        return False

    with open(PROMETHEUS_YML, "w") as f:
        f.write(new_content)

    return True


# ---------------------------------------------------------------------------
# 5. prometheus/rules/auto-discovered.yml
# ---------------------------------------------------------------------------

def remove_from_alert_rules(service_name: str) -> bool:
    """
    Remove the alert rule for this service from auto-discovered.yml.

    docker-watcher names alerts: <Capitalized>Down (e.g. MyApiDown).
    Uses regex on raw text to avoid yaml.dump rewriting `rules:` as `rules: []`,
    which would break the watcher's raw-text append on the next ADD.
    Returns True if a rule was found and removed.
    """
    if not os.path.exists(AUTO_DISC_FILE):
        return False

    with open(AUTO_DISC_FILE, "r") as f:
        content = f.read()

    alert_name = service_name.replace("-", "").replace("_", "").title() + "Down"

    # Match the alert block from `  - alert: <name>` to the next `  - alert:` or EOF
    pattern = (
        rf'\n  - alert: {re.escape(alert_name)}\n'
        rf'.*?'
        rf'(?=\n  - alert:|\Z)'
    )
    new_content, count = re.subn(pattern, "", content, flags=re.DOTALL)

    if count == 0:
        return False

    with open(AUTO_DISC_FILE, "w") as f:
        f.write(new_content)

    return True


# ---------------------------------------------------------------------------
# Prometheus reload
# ---------------------------------------------------------------------------

def restart_watcher() -> bool:
    """
    Restart docker-watcher so its in-memory already_processed set is cleared.
    This allows the watcher to re-register any service that was cleaned from
    config files and then re-added.
    Returns True on success.
    """
    try:
        result = subprocess.run(
            ["docker", "restart", "docker-watcher"],
            capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0
    except Exception:
        return False


def reload_prometheus() -> bool:
    """
    Send POST /-/reload to Prometheus to pick up config changes.
    Returns True on success.
    """
    try:
        r = requests.post(f"{PROMETHEUS_URL}/-/reload", timeout=5)
        return r.status_code == 200
    except requests.RequestException:
        return False


# ---------------------------------------------------------------------------
# Main entry point — run full cleanup for a service
# ---------------------------------------------------------------------------

def remove_service(service_name: str) -> dict:
    """
    Run the full REMOVE flow for a service.

    Steps:
      1. Stop and remove the container via docker compose
      2. Remove from docker-compose.yml
      3. Remove from rules.py
      4. Remove from action-executor/main.py
      5. Remove from prometheus.yml
      6. Remove from auto-discovered.yml
      7. Reload Prometheus

    Returns a dict with per-step results for reporting to the user.
    """
    results = {}

    # Step 1 — stop container
    stop = subprocess.run(
        ["docker", "compose", "stop", service_name],
        cwd=COMPOSE_DIR, capture_output=True, text=True
    )
    rm = subprocess.run(
        ["docker", "compose", "rm", "-f", service_name],
        cwd=COMPOSE_DIR, capture_output=True, text=True
    )
    results["container_stopped"] = stop.returncode == 0
    results["container_removed"] = rm.returncode == 0

    # Steps 2–6
    results["compose_cleaned"]   = remove_from_compose(service_name)
    results["rules_cleaned"]     = remove_from_rules(service_name)
    results["action_cleaned"]    = remove_from_action_map(service_name)
    results["prometheus_cleaned"] = remove_from_prometheus(service_name)
    results["alerts_cleaned"]    = remove_from_alert_rules(service_name)

    # Step 7 — reload Prometheus
    results["prometheus_reloaded"] = reload_prometheus()

    # Step 8 — restart docker-watcher so its in-memory state is reset
    # (prevents cleaned services from being skipped on re-add)
    results["watcher_restarted"] = restart_watcher()

    return results
