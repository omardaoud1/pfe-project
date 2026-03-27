"""
agent.py — DockerAgent CLI with YAML preview and inline validation.

Flow (ADD):
  add → name → image → port → probe → volumes → depends_on
  → YAML preview + confirm (yes/no/cancel) → execute

Flow (REMOVE):
  remove → service name → confirm → execute + report

Special commands (available at any prompt):
  list    — show services in docker-compose.yml
  status  — show running containers (docker compose ps)
  reset   — restart the current conversation
  quit    — exit
"""

import logging
import os
import yaml
from conversation_manager import ConversationManager, Step
import validator
import docker_manager
import cleanup_manager

# ---------------------------------------------------------------------------
# Action log
# ---------------------------------------------------------------------------

_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_log_dir, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(_log_dir, "agent.log"),
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def show_yaml_preview(info) -> str:
    """Build and return a formatted YAML preview of the service to be added."""
    cport = info.container_port or info.port
    block = docker_manager.build_service_block(
        name=info.name,
        image=info.image,
        port=info.port,
        container_port=cport,
        probe=info.probe,
        restart=info.restart,
        env=info.env or None,
        volumes=info.volumes or None,
        depends_on=info.depends_on or None,
        command=info.command or None,
    )
    block_yaml = yaml.dump(
        {info.name: block},
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    lines = [
        "┌─────────────────────────────────────────┐",
        "│         Service YAML Preview            │",
        "└─────────────────────────────────────────┘",
    ]
    for line in block_yaml.splitlines():
        lines.append(f"  {line}")
    lines.append("")
    lines.append("  Summary:")
    lines.append(f"  • Name:        {info.name}")
    lines.append(f"  • Image:       {info.image}")
    lines.append(f"  • Host port:   {info.port}")
    lines.append(f"  • Cont. port:  {info.container_port or info.port}  (port mapping: {info.port}:{info.container_port or info.port})")
    lines.append(f"  • Probe:       {info.probe}")
    lines.append(f"  • Restart:     {info.restart}")
    lines.append(f"  • Env vars:    {', '.join(info.env) if info.env else '—'}")
    lines.append(f"  • Volumes:     {', '.join(info.volumes) if info.volumes else '—'}")
    lines.append(f"  • Depends on:  {', '.join(info.depends_on) if info.depends_on else '—'}")
    lines.append(f"  • Command:     {info.command if info.command else '—'}")
    return "\n".join(lines)


def run_add(info) -> str:
    _log.info(
        "ADD  name=%s  image=%s  port=%d  probe=%s  restart=%s  "
        "env=%s  volumes=%s  depends_on=%s  command=%s",
        info.name, info.image, info.port, info.probe, info.restart,
        info.env, info.volumes, info.depends_on, info.command,
    )
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
        _log.error("ADD FAILED  name=%s  output=%s", info.name, output)
        return f"✗ docker compose up failed:\n{output}"

    print(f"\nAgent: Waiting 35s for docker-watcher to register '{info.name}'...")
    docker_manager.wait_for_watcher(35)

    reloaded = cleanup_manager.reload_prometheus()
    _log.info("ADD OK  name=%s  port=%d  prometheus_reloaded=%s", info.name, info.port, reloaded)
    return (
        f"✓ Service '{info.name}' is now running on port {info.port}.\n"
        f"\n"
        f"  docker-watcher has automatically registered it in:\n"
        f"    • decision-engine/app/rules.py\n"
        f"    • action-executor/main.py\n"
        f"    • prometheus/prometheus.yml\n"
        f"    • prometheus/rules/auto-discovered.yml\n"
        f"\n"
        f"  {'✓' if reloaded else '✗'} Prometheus reloaded — monitoring pipeline is now active for '{info.name}'."
    )


def run_remove(service_name: str) -> str:
    _log.info("REMOVE  name=%s", service_name)
    results = cleanup_manager.remove_service(service_name)
    ok  = "✓"
    nok = "✗"
    all_ok = all(results.get(k) for k in (
        "compose_cleaned", "rules_cleaned", "action_cleaned",
        "prometheus_cleaned", "alerts_cleaned",
    ))
    _log.info("REMOVE %s  name=%s  results=%s", "OK" if all_ok else "PARTIAL", service_name, results)
    lines = [
        f"Service '{service_name}' removal report:",
        f"  {ok if results['container_stopped'] else nok}  Container stopped",
        f"  {ok if results['container_removed'] else nok}  Container removed",
        f"  {ok if results['compose_cleaned'] else nok}  docker-compose.yml",
        f"  {ok if results['rules_cleaned'] else nok}  decision-engine/rules.py",
        f"  {ok if results['action_cleaned'] else nok}  action-executor/main.py",
        f"  {ok if results['prometheus_cleaned'] else nok}  prometheus/prometheus.yml",
        f"  {ok if results['alerts_cleaned'] else nok}  auto-discovered.yml",
        f"  {ok if results['prometheus_reloaded'] else nok}  Prometheus reloaded",
        f"  {ok if results['watcher_restarted'] else nok}  docker-watcher restarted",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║        DockerAgent  v1.0                 ║")
    print("  ║   Infrastructure Service Manager         ║")
    print("  ╚══════════════════════════════════════════╝")
    print()

    cm = ConversationManager()
    print(f"Agent: {cm.current_question()}\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nAgent: Goodbye.")
            break

        if not user_input and cm.step not in (
            Step.ADD_ASK_CONTAINER_PORT,
            Step.ADD_ASK_PROBE,   Step.ADD_ASK_RESTART,
            Step.ADD_ASK_ENV,     Step.ADD_ASK_VOLUMES,
            Step.ADD_ASK_DEPENDS, Step.ADD_ASK_COMMAND,
        ):
            continue

        if user_input.lower() in ("quit", "exit"):
            print("Agent: Goodbye.")
            break

        if user_input.lower() == "reset":
            cm.reset()
            print(f"\nAgent: {cm.current_question()}\n")
            continue

        if user_input.lower() in ("list", "services"):
            services = validator.get_existing_services()
            if services:
                print(f"\nAgent: Services in docker-compose.yml:")
                for svc in services:
                    print(f"  • {svc}")
            else:
                print("\nAgent: No services found in docker-compose.yml.")
            print()
            continue

        if user_input.lower() == "status":
            output = docker_manager.get_status()
            print(f"\nAgent: Container status:\n{output}\n")
            continue

        step, error = cm.process(user_input)

        if error:
            print(f"\nAgent: ⚠  {error}")
            print(f"Agent: {cm.current_question()}\n")

        elif step == Step.ADD_CONFIRM:
            # Show YAML preview before asking for confirmation
            print(f"\n{show_yaml_preview(cm.service_info)}\n")
            print(f"Agent: {cm.current_question()}\n")

        elif step == Step.READY_TO_ADD:
            result = run_add(cm.service_info)
            print(f"\nAgent: {result}\n")
            cm.reset()
            print(f"Agent: {cm.current_question()}\n")

        elif step == Step.READY_TO_REMOVE:
            result = run_remove(cm.remove_target)
            print(f"\nAgent: {result}\n")
            cm.reset()
            print(f"Agent: {cm.current_question()}\n")

        elif step == Step.REMOVE_ASK_CONFIRM:
            locs = cm.remove_locations
            found_in = [f for f, v in locs.items() if v]
            not_in   = [f for f, v in locs.items() if not v]
            print(f"\n  ⚠  Service '{cm.remove_target}' found in:")
            for f in found_in:
                print(f"     ✓  {f}")
            if not_in:
                print(f"     —  Not registered in: {', '.join(not_in)}")
            print()
            print(f"Agent: {cm.current_question()}\n")

        elif cm.current_question():
            print(f"\nAgent: {cm.current_question()}\n")


if __name__ == "__main__":
    main()
