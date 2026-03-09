"""
validator.py — Input validation before any action is taken.

Checks:
  - Is the service name already used in docker-compose.yml?
  - Is the port already taken in docker-compose.yml?

The agent calls these before writing anything to disk.
No changes are made here — pure read-only checks.
"""

import yaml
import re
import os

BASE         = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMPOSE_FILE = os.path.join(BASE, "monitoring", "docker-compose.yml")
RULES_FILE   = os.path.join(BASE, "decision-engine", "app", "rules.py")
ACTION_FILE  = os.path.join(BASE, "action-executor", "main.py")
PROM_FILE    = os.path.join(BASE, "monitoring", "prometheus", "prometheus.yml")
AUTO_FILE    = os.path.join(BASE, "monitoring", "prometheus", "rules", "auto-discovered.yml")


def _load_compose() -> dict:
    with open(COMPOSE_FILE, "r") as f:
        return yaml.safe_load(f)


def service_exists(name: str) -> bool:
    """
    Return True if a service with this name already exists in docker-compose.yml.

    Example:
        service_exists("redis")   → True
        service_exists("my-api")  → False (not yet added)
    """
    data = _load_compose()
    services = data.get("services", {})
    return name in services


def port_taken(port: int) -> bool:
    """
    Return True if this host port is already mapped in docker-compose.yml.

    docker-compose ports entries can be:
      - "9090:9090"   (string)
      - 9090          (int, rare)
      - {"target": 9090, "published": 9090}  (dict, rare)

    We check the host-side (left side of the colon).

    Example:
        port_taken(9090)  → True  (Prometheus uses it)
        port_taken(9999)  → False
    """
    data = _load_compose()
    services = data.get("services", {})

    for svc in services.values():
        for entry in svc.get("ports", []):
            if isinstance(entry, str):
                host_port = entry.split(":")[0]
                if str(port) == host_port:
                    return True
            elif isinstance(entry, int):
                if port == entry:
                    return True
            elif isinstance(entry, dict):
                if str(port) == str(entry.get("published", "")):
                    return True
    return False


def get_used_ports() -> list[int]:
    """Return all host ports currently in use, as a sorted list of ints."""
    data = _load_compose()
    services = data.get("services", {})
    used = []
    for svc in services.values():
        for entry in svc.get("ports", []):
            try:
                if isinstance(entry, str):
                    used.append(int(entry.split(":")[0]))
                elif isinstance(entry, int):
                    used.append(entry)
                elif isinstance(entry, dict):
                    used.append(int(entry.get("published", 0)))
            except (ValueError, TypeError):
                pass
    return sorted(set(used))


def get_existing_services() -> list[str]:
    """Return all service names currently defined in docker-compose.yml."""
    data = _load_compose()
    return list(data.get("services", {}).keys())


def find_service_across_files(name: str) -> dict:
    """
    Check all config files for traces of a service name.
    Used for REMOVE validation — a service may have been registered
    by docker-watcher even if it was removed from docker-compose.yml.

    Returns a dict: { file_label: bool } showing where the service was found.
    """
    found = {
        "docker-compose.yml": False,
        "rules.py":           False,
        "action-executor/main.py": False,
        "prometheus.yml":     False,
        "auto-discovered.yml": False,
    }

    # docker-compose.yml
    try:
        data = _load_compose()
        found["docker-compose.yml"] = name in data.get("services", {})
    except Exception:
        pass

    # rules.py — look for: service == "<name>"
    try:
        with open(RULES_FILE, "r") as f:
            content = f.read()
        found["rules.py"] = f'service == "{name}"' in content
    except Exception:
        pass

    # action-executor/main.py — look for action key
    try:
        action_key = "restart_" + name.replace("-", "_")
        with open(ACTION_FILE, "r") as f:
            content = f.read()
        found["action-executor/main.py"] = f'"{action_key}"' in content
    except Exception:
        pass

    # prometheus.yml — look for blackbox job
    try:
        with open(PROM_FILE, "r") as f:
            content = f.read()
        found["prometheus.yml"] = f'blackbox_{name}' in content
    except Exception:
        pass

    # auto-discovered.yml
    try:
        with open(AUTO_FILE, "r") as f:
            content = f.read()
        found["auto-discovered.yml"] = name in content
    except Exception:
        pass

    return found


def service_removable(name: str) -> tuple[bool, dict]:
    """
    Return (found_anywhere, locations_dict).
    found_anywhere=True if the service exists in at least one config file.
    """
    locations = find_service_across_files(name)
    return any(locations.values()), locations
