"""
docker_manager.py — Reads and writes docker-compose.yml, runs docker compose.

Responsibilities:
  1. Read the current docker-compose.yml to know existing services/ports.
  2. Write a new service block into docker-compose.yml (ADD flow).
  3. Run `docker compose up -d <service>` to start the new container.
  4. Wait ~35 seconds for docker-watcher to detect it, then confirm.

Key rule from the plan:
  The LLM never writes YAML. Python builds the block from collected values.
  The agent inserts it before the `volumes:` section.
"""

import os
import subprocess
import time
import yaml

COMPOSE_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "monitoring", "docker-compose.yml")
)
COMPOSE_DIR = os.path.dirname(COMPOSE_FILE)


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def get_services() -> dict:
    """Return the full services dict from docker-compose.yml."""
    with open(COMPOSE_FILE, "r") as f:
        data = yaml.safe_load(f)
    return data.get("services", {})


def get_service_info(name: str) -> dict | None:
    """Return the config dict for a single service, or None if not found."""
    return get_services().get(name)


# ---------------------------------------------------------------------------
# Write — ADD flow
# ---------------------------------------------------------------------------

def build_service_block(
    name: str,
    image: str,
    port: int,
    container_port: int | None = None,
    probe: str = "http",
    restart: str = "unless-stopped",
    env: list[str] | None = None,
    volumes: list[str] | None = None,
    depends_on: list[str] | None = None,
    command: str | None = None,
) -> dict:
    """Build the service dict to be inserted into docker-compose.yml."""
    cport = container_port or port   # internal port the service actually listens on
    block: dict = {
        "image": image,
        "container_name": name,
        "ports": [f"{port}:{cport}"],
        "labels": [
            f"monitoring.port={cport}",   # container port — used by Prometheus blackbox
            f"monitoring.probe={probe}",
        ],
        "restart": restart,
    }

    if env:
        block["environment"] = env

    if volumes:
        block["volumes"] = volumes

    if depends_on:
        block["depends_on"] = depends_on

    if command:
        block["command"] = command

    return block


def add_service(
    name: str,
    image: str,
    port: int,
    container_port: int | None = None,
    probe: str = "http",
    restart: str = "unless-stopped",
    env: list[str] | None = None,
    volumes: list[str] | None = None,
    depends_on: list[str] | None = None,
    command: str | None = None,
) -> None:
    """
    Insert a new service block into docker-compose.yml before the `volumes:` section.

    Reads the file as raw text to preserve formatting and comments,
    then injects the new block just before the `volumes:` line.
    """
    block = build_service_block(name, image, port, container_port, probe, restart, env, volumes, depends_on, command)

    # Serialize just the new service block (indented 2 spaces inside services:)
    block_yaml = yaml.dump(
        {name: block},
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    # Indent each line by 2 spaces (to sit inside `services:`)
    indented = "\n".join("  " + line for line in block_yaml.splitlines())

    # Read raw file text
    with open(COMPOSE_FILE, "r") as f:
        content = f.read()

    # Insert before the `volumes:` section
    if "\nvolumes:" in content:
        content = content.replace("\nvolumes:", f"\n{indented}\n\nvolumes:", 1)
    else:
        # No volumes section — append at end
        content = content.rstrip() + f"\n\n{indented}\n"

    with open(COMPOSE_FILE, "w") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Docker compose commands
# ---------------------------------------------------------------------------

def compose_up(service_name: str) -> tuple[bool, str]:
    """
    Run `docker compose up -d <service_name>` from the monitoring/ directory.

    Returns:
        (success: bool, output: str)
    """
    result = subprocess.run(
        ["docker", "compose", "up", "-d", service_name],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    return result.returncode == 0, output.strip()


def compose_down(service_name: str) -> tuple[bool, str]:
    """
    Run `docker compose stop <service_name> && docker compose rm -f <service_name>`.

    Returns:
        (success: bool, output: str)
    """
    stop = subprocess.run(
        ["docker", "compose", "stop", service_name],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
    )
    rm = subprocess.run(
        ["docker", "compose", "rm", "-f", service_name],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
    )
    output = (stop.stdout + stop.stderr + rm.stdout + rm.stderr).strip()
    success = stop.returncode == 0 and rm.returncode == 0
    return success, output


def get_status() -> str:
    """
    Run `docker compose ps` and return the formatted output.
    Shows container name, status, and ports for all services.
    """
    result = subprocess.run(
        ["docker", "compose", "ps"],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
    )
    return (result.stdout or result.stderr).strip()


def wait_for_watcher(seconds: int = 35) -> None:
    """
    Wait for docker-watcher to detect the new container and update
    rules.py, main.py, prometheus.yml, and auto-discovered.yml.
    The watcher runs every 30 seconds, so 35s guarantees at least one cycle.
    """
    time.sleep(seconds)
