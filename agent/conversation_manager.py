"""
conversation_manager.py — Step-by-step form with inline validation.

Each step validates the input immediately and re-asks if invalid.
process() returns (step, error_message) — if error is not None,
agent.py re-prints the current question so the user can retry.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
import re
import validator


class Step(Enum):
    ASK_INTENT         = auto()
    ADD_ASK_NAME       = auto()
    ADD_ASK_IMAGE      = auto()
    ADD_ASK_PORT            = auto()
    ADD_ASK_CONTAINER_PORT  = auto()
    ADD_ASK_PROBE           = auto()
    ADD_ASK_RESTART    = auto()
    ADD_ASK_ENV        = auto()
    ADD_ASK_VOLUMES    = auto()
    ADD_ASK_DEPENDS    = auto()
    ADD_ASK_COMMAND    = auto()
    ADD_CONFIRM        = auto()
    READY_TO_ADD       = auto()
    REMOVE_ASK_NAME    = auto()
    REMOVE_ASK_CONFIRM = auto()
    READY_TO_REMOVE    = auto()


QUESTIONS = {
    Step.ASK_INTENT:         "What do you want to do?\n  → add / remove\n  · list · status · reset · quit",
    Step.ADD_ASK_NAME:       "Service name?",
    Step.ADD_ASK_IMAGE:      "Docker image? (e.g. nginx:latest, redis:7)",
    Step.ADD_ASK_PORT:           "Host port? (exposed on the host, e.g. 8082)",
    Step.ADD_ASK_CONTAINER_PORT: "Container port? (what the service listens on inside Docker, e.g. 80) [Enter = same as host port]",
    Step.ADD_ASK_PROBE:          "Monitoring probe type? (http / tcp) [Enter = http]",
    Step.ADD_ASK_RESTART:    "Restart policy? (always / unless-stopped / on-failure / no) [Enter = unless-stopped]",
    Step.ADD_ASK_ENV:        "Environment variables? (e.g. KEY=VALUE,KEY2=VALUE2) [Enter to skip]",
    Step.ADD_ASK_VOLUMES:    "Volumes? (e.g. /host/path:/container/path) [Enter to skip]",
    Step.ADD_ASK_DEPENDS:    "Depends on which service? (e.g. redis, rabbitmq) [Enter to skip]",
    Step.ADD_ASK_COMMAND:    "Custom command/entrypoint? [Enter to skip]",
    Step.ADD_CONFIRM:        "Confirm and apply? (yes / no / cancel)",
    Step.REMOVE_ASK_NAME:    "Which service do you want to remove?",
    Step.REMOVE_ASK_CONFIRM: "Are you sure? This will stop the container and remove it from all config files. (yes / no)",
}


@dataclass
class ServiceInfo:
    name:           str  = ""
    image:          str  = ""
    port:           int  = 0   # host port
    container_port: int  = 0   # container port (0 = same as host port)
    probe:          str  = "http"
    restart:    str  = "unless-stopped"
    env:        list = field(default_factory=list)   # ["KEY=VALUE", ...]
    volumes:    list = field(default_factory=list)
    depends_on: list = field(default_factory=list)
    command:    str  = ""


class ConversationManager:
    def __init__(self):
        self.step             = Step.ASK_INTENT
        self.service_info     = ServiceInfo()
        self.remove_target    = ""
        self.remove_locations = {}

    def current_question(self) -> str:
        return QUESTIONS.get(self.step, "")

    def process(self, user_input: str) -> tuple:
        """
        Process user input for the current step.
        Returns (step, error_message).
        If error_message is not None → validation failed, step unchanged, re-ask.
        """
        val  = user_input.strip()
        skip = val == "" or val.lower() == "skip"

        # ── Intent ──────────────────────────────────────────────────────
        if self.step == Step.ASK_INTENT:
            if "add" in val.lower():
                self.step = Step.ADD_ASK_NAME
            elif "remove" in val.lower() or "delete" in val.lower():
                self.step = Step.REMOVE_ASK_NAME
            else:
                return self.step, "Please type 'add' or 'remove'."
            return self.step, None

        # ── ADD: name ───────────────────────────────────────────────────
        elif self.step == Step.ADD_ASK_NAME:
            if not val:
                return self.step, "Service name cannot be empty. Please enter a name."
            if not re.fullmatch(r'[a-z0-9]([a-z0-9-]*[a-z0-9])?', val):
                return self.step, (
                    "Service name must use only lowercase letters, digits, and hyphens "
                    "(e.g. my-api, redis2). Cannot start or end with a hyphen."
                )
            if validator.service_exists(val):
                existing = validator.get_existing_services()
                return self.step, (
                    f"'{val}' already exists. "
                    f"Existing services: {', '.join(existing)}. "
                    f"Please choose a different name."
                )
            self.service_info.name = val
            self.step = Step.ADD_ASK_IMAGE
            return self.step, None

        # ── ADD: image ──────────────────────────────────────────────────
        elif self.step == Step.ADD_ASK_IMAGE:
            if not val:
                return self.step, "Image cannot be empty. Example: nginx:latest"
            if ":" not in val:
                return self.step, (
                    f"Image should include a tag (e.g. nginx:latest, redis:7). "
                    f"Please re-enter."
                )
            self.service_info.image = val
            self.step = Step.ADD_ASK_PORT
            return self.step, None

        # ── ADD: port ───────────────────────────────────────────────────
        elif self.step == Step.ADD_ASK_PORT:
            if not val.isdigit():
                return self.step, "Port must be a number (e.g. 8082). Please re-enter."
            port = int(val)
            if port < 1 or port > 65535:
                return self.step, "Port must be between 1 and 65535. Please re-enter."
            if validator.port_taken(port):
                used = validator.get_used_ports()
                return self.step, (
                    f"Port {port} is already in use. "
                    f"Used ports: {used}. Please choose a different port."
                )
            self.service_info.port = port
            self.step = Step.ADD_ASK_CONTAINER_PORT
            return self.step, None

        # ── ADD: container port ─────────────────────────────────────────
        elif self.step == Step.ADD_ASK_CONTAINER_PORT:
            if skip:
                self.service_info.container_port = self.service_info.port
            elif val.isdigit():
                cport = int(val)
                if cport < 1 or cport > 65535:
                    return self.step, "Container port must be between 1 and 65535."
                self.service_info.container_port = cport
            else:
                return self.step, "Container port must be a number (e.g. 80). Press Enter to use the same as host port."
            self.step = Step.ADD_ASK_PROBE
            return self.step, None

        # ── ADD: probe ──────────────────────────────────────────────────
        elif self.step == Step.ADD_ASK_PROBE:
            if skip:
                self.service_info.probe = "http"
            elif val.lower() in ("http", "tcp"):
                self.service_info.probe = val.lower()
            else:
                return self.step, "Please enter 'http', 'tcp', or press Enter for default (http)."
            self.step = Step.ADD_ASK_RESTART
            return self.step, None

        # ── ADD: restart policy ─────────────────────────────────────────
        elif self.step == Step.ADD_ASK_RESTART:
            valid = ("always", "unless-stopped", "on-failure", "no")
            if skip:
                self.service_info.restart = "unless-stopped"
            elif val.lower() in valid:
                self.service_info.restart = val.lower()
            else:
                return self.step, f"Invalid restart policy. Choose from: {', '.join(valid)}. Or press Enter for default (unless-stopped)."
            self.step = Step.ADD_ASK_ENV
            return self.step, None

        # ── ADD: environment variables (optional) ───────────────────────
        elif self.step == Step.ADD_ASK_ENV:
            if not skip:
                pairs = [e.strip() for e in val.split(",")]
                invalid = [p for p in pairs if "=" not in p]
                if invalid:
                    return self.step, f"Invalid format: {invalid}. Use KEY=VALUE format (e.g. DEBUG=true,PORT=8080). Or press Enter to skip."
                self.service_info.env = pairs
            self.step = Step.ADD_ASK_VOLUMES
            return self.step, None

        # ── ADD: volumes (optional) ─────────────────────────────────────
        elif self.step == Step.ADD_ASK_VOLUMES:
            if not skip:
                self.service_info.volumes = [v.strip() for v in val.split(",")]
            self.step = Step.ADD_ASK_DEPENDS
            return self.step, None

        # ── ADD: depends_on (optional) ──────────────────────────────────
        elif self.step == Step.ADD_ASK_DEPENDS:
            if not skip:
                deps = [d.strip() for d in val.split(",")]
                missing = [d for d in deps if not validator.service_exists(d)]
                if missing:
                    return self.step, (
                        f"These services don't exist: {', '.join(missing)}. "
                        f"Press Enter to skip, or enter valid service names."
                    )
                self.service_info.depends_on = deps
            self.step = Step.ADD_ASK_COMMAND
            return self.step, None

        # ── ADD: custom command (optional) ──────────────────────────────
        elif self.step == Step.ADD_ASK_COMMAND:
            if not skip:
                self.service_info.command = val
            self.step = Step.ADD_CONFIRM
            return self.step, None

        # ── ADD: final confirmation ──────────────────────────────────────
        elif self.step == Step.ADD_CONFIRM:
            if val.lower() in ("yes", "y"):
                self.step = Step.READY_TO_ADD
            elif val.lower() in ("no", "n"):
                # Let user restart from name
                self.service_info = ServiceInfo()
                self.step = Step.ADD_ASK_NAME
                return self.step, "Restarting. Let's try again."
            elif val.lower() == "cancel":
                self.reset()
                return self.step, "Cancelled."
            else:
                return self.step, "Please type 'yes' to confirm, 'no' to restart, or 'cancel' to abort."
            return self.step, None

        # ── REMOVE: name ────────────────────────────────────────────────
        elif self.step == Step.REMOVE_ASK_NAME:
            if not val:
                return self.step, "Please enter the service name."
            found, locations = validator.service_removable(val)
            if not found:
                existing = validator.get_existing_services()
                return self.step, (
                    f"'{val}' was not found in any config file "
                    f"(docker-compose.yml, rules.py, main.py, prometheus.yml).\n"
                    f"  Services in docker-compose.yml: {', '.join(existing)}.\n"
                    f"  Please re-enter."
                )
            self.remove_target = val
            self.remove_locations = locations
            self.step = Step.REMOVE_ASK_CONFIRM
            return self.step, None

        # ── REMOVE: confirm ─────────────────────────────────────────────
        elif self.step == Step.REMOVE_ASK_CONFIRM:
            if val.lower() in ("yes", "y", "confirm", "ok", "sure"):
                self.step = Step.READY_TO_REMOVE
            else:
                self.step = Step.ASK_INTENT
                self.remove_target = ""
                return self.step, "Removal cancelled."
            return self.step, None

        return self.step, None

    def reset(self):
        self.__init__()
