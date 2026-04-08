"""
log_analyzer.py — Fetch container logs via Docker SDK.

Given a service name and optional tail count, returns raw log text.
Also resolves the correct container name (handles cases where the user
types the service name but the container name differs slightly).
"""

import docker


def get_logs(service: str, tail: int = 100) -> tuple[str, str]:
    """
    Fetch the last `tail` lines of logs for `service`.

    Returns (raw_logs, error_message).
    - On success: (log_text, "")
    - On failure: ("", error_description)
    """
    try:
        client = docker.from_env()
    except Exception as e:
        return "", f"Cannot connect to Docker: {e}"

    # Try exact name first, then prefix match
    container = None
    try:
        container = client.containers.get(service)
    except docker.errors.NotFound:
        # Try to find by partial name match
        all_containers = client.containers.list(all=True)
        matches = [c for c in all_containers if service.lower() in c.name.lower()]
        if len(matches) == 1:
            container = matches[0]
        elif len(matches) > 1:
            names = ", ".join(c.name for c in matches)
            return "", f"Ambiguous service name '{service}'. Matches: {names}. Please be more specific."
        else:
            all_names = [c.name for c in client.containers.list(all=True)]
            return "", (
                f"Service '{service}' not found. "
                f"Running containers: {', '.join(all_names) if all_names else 'none'}."
            )

    try:
        raw = container.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
        if not raw.strip():
            return f"[No logs in last {tail} lines for '{container.name}']", ""
        return raw, ""
    except Exception as e:
        return "", f"Failed to read logs for '{container.name}': {e}"


def list_services() -> list[str]:
    """Return names of all containers (running and stopped)."""
    try:
        client = docker.from_env()
        return [c.name for c in client.containers.list(all=True)]
    except Exception:
        return []
