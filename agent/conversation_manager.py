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


# Well-known container port for each image base name.
# Key = image name without tag/registry, value = default internal port.
KNOWN_IMAGE_PORTS: dict[str, int] = {
    "redis":         6379,
    "postgres":      5432,
    "postgresql":    5432,
    "mysql":         3306,
    "mariadb":       3306,
    "mongo":         27017,
    "mongodb":       27017,
    "rabbitmq":      5672,
    "nginx":         80,
    "apache":        80,
    "httpd":         80,
    "elasticsearch": 9200,
    "kibana":        5601,
    "grafana":       3000,
    "prometheus":    9090,
    "memcached":     11211,
    "cassandra":     9042,
    "zookeeper":     2181,
    "kafka":         9092,
    "influxdb":      8086,
    "minio":         9000,
    "vault":         8200,
    "consul":        8500,
    "jenkins":       8080,
    "sonarqube":     9000,
    "wordpress":     80,
    "drupal":        80,
    "joomla":        80,
    "traefik":       80,
    "haproxy":       80,
    "node":          3000,
    "python":        8000,
    "golang":        8080,
    "php":           80,
    "tomcat":        8080,
    "wildfly":       8080,
}


def _known_port(image: str) -> int | None:
    """Return the well-known container port for an image, or None if unknown."""
    # Strip registry prefix and tag: "docker.io/library/redis:7" → "redis"
    base = image.split("/")[-1].split(":")[0].lower()
    return KNOWN_IMAGE_PORTS.get(base)


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
    # ── Log Analysis ─────────────────────────────────────────────────────────
    LOGS_ASK_SERVICE   = auto()   # ask which service to analyze
    LOGS_ASK_TAIL      = auto()   # ask how many lines (optional)
    READY_TO_ANALYZE   = auto()   # trigger analysis in api.py


QUESTIONS = {
    Step.ASK_INTENT:         "Hello! I'm your Docker Agent 🐳\nWhat would you like to do?\n(Type *help* to see all available options)",
    Step.ADD_ASK_NAME:       "Service name?\n(lowercase letters, digits, hyphens — e.g. my-api)",
    Step.ADD_ASK_IMAGE:      "Docker image?\n(must include a tag — e.g. nginx:latest, redis:7)",
    Step.ADD_ASK_PORT:           "Host port?\n(port exposed on the host — e.g. 8082)",
    Step.ADD_ASK_CONTAINER_PORT: "Container port?\n(port the service listens on inside Docker — e.g. 80)\nType 'skip' to use the same as host port.",
    Step.ADD_ASK_PROBE:          "Monitoring probe type?\n  http  or  tcp\nType 'skip' for default (http).",
    Step.ADD_ASK_RESTART:    "Restart policy?\n  always / unless-stopped / on-failure / no\nType 'skip' for default (unless-stopped).",
    Step.ADD_ASK_ENV:        "Environment variables?\n(e.g. KEY=VALUE,KEY2=VALUE2)\nType 'skip' if none.",
    Step.ADD_ASK_VOLUMES:    "Volumes?\n(e.g. /host/path:/container/path)\nType 'skip' if none.",
    Step.ADD_ASK_DEPENDS:    "Depends on which service?\n(e.g. redis, rabbitmq)\nType 'skip' if none.",
    Step.ADD_ASK_COMMAND:    "Custom command / entrypoint?\nType 'skip' if none.",
    Step.ADD_CONFIRM:        "Confirm and apply?\n  yes  —  deploy the service\n  no   —  change the config\n  cancel  —  abort",
    Step.REMOVE_ASK_NAME:    "Which service do you want to remove?",
    Step.REMOVE_ASK_CONFIRM: "Are you sure?\nThis will stop the container and remove it from all config files.\n  yes  —  confirm removal\n  no   —  cancel",
    Step.LOGS_ASK_SERVICE:   "Which service do you want to analyze?\n(e.g. nginx, redis, postgres)\nType 'list' to see running containers.",
    Step.LOGS_ASK_TAIL:      "How many log lines to analyze?\n(e.g. 100, 200, 500)\nType 'skip' for default (100).",
}


@dataclass
class LogRequest:
    service: str = ""
    tail:    int = 100


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
        self.log_request      = LogRequest()

    def current_question(self) -> str:
        if self.step == Step.ADD_ASK_CONTAINER_PORT and self.service_info.image:
            known = _known_port(self.service_info.image)
            image_base = self.service_info.image.split("/")[-1].split(":")[0]
            if known:
                return (
                    f"Container port?\n"
                    f"'{image_base}' listens on port {known} by default.\n"
                    f"Type {known} or 'skip' to use {known} (recommended)."
                )
            else:
                return (
                    f"Container port?\n"
                    f"(unknown image — enter the port your app listens on inside Docker)\n"
                    f"Type 'skip' to use the same as host port."
                )
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
            low = val.lower()
            if "add" in low:
                self.step = Step.ADD_ASK_NAME
            elif "remove" in low or "delete" in low:
                self.step = Step.REMOVE_ASK_NAME
            elif self._is_log_intent(low):
                # Smart: if service name given inline, skip LOGS_ASK_SERVICE
                service = self._extract_service_from_log_intent(low)
                tail    = self._extract_tail_from_log_intent(low)
                if service:
                    self.log_request.service = service
                    self.log_request.tail    = tail
                    self.step = Step.READY_TO_ANALYZE
                else:
                    self.log_request = LogRequest()
                    self.step = Step.LOGS_ASK_SERVICE
            else:
                # unrecognized input → hint to use help
                return self.step, "I didn't understand that. Type *help* to see what I can do, or just tell me what you need."
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
            known = _known_port(self.service_info.image)
            image_base = self.service_info.image.split("/")[-1].split(":")[0]

            if skip:
                # For known images, always use the correct default port
                if known:
                    self.service_info.container_port = known
                else:
                    self.service_info.container_port = self.service_info.port
            elif val.isdigit():
                cport = int(val)
                if cport < 1 or cport > 65535:
                    return self.step, "Container port must be between 1 and 65535."
                # Validate against known image port
                if known and cport != known:
                    return self.step, (
                        f"Wrong port for '{image_base}' — port {cport} won't work.\n"
                        f"'{image_base}' listens on port {known} inside the container.\n"
                        f"Use {known} as the container port, or your service won't appear in monitoring.\n"
                        f"Type {known} or 'skip' to use the correct default."
                    )
                if not known:
                    # Unknown image — accept the port but warn
                    self.service_info.container_port = cport
                    self.step = Step.ADD_ASK_PROBE
                    # Return None so the warning appears via the question, not an error.
                    # We store a one-time notice in the step question override below.
                    return self.step, None
                self.service_info.container_port = cport
            else:
                return self.step, "Container port must be a number (e.g. 80). Type 'skip' to use the image default."
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
            aliases = {"unless-stoped": "unless-stopped", "onfailure": "on-failure", "on_failure": "on-failure"}
            if skip:
                self.service_info.restart = "unless-stopped"
            else:
                v = aliases.get(val.lower(), val.lower())
                if v in valid:
                    self.service_info.restart = v
                else:
                    return self.step, f"Invalid restart policy. Choose from: {', '.join(valid)}. Type 'skip' for default (unless-stopped)."
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

        # ── LOGS: ask service ────────────────────────────────────────────
        elif self.step == Step.LOGS_ASK_SERVICE:
            if not val:
                return self.step, "Please enter a service name (e.g. nginx, redis)."
            tail    = self._extract_tail_from_log_intent(val.lower())
            service = self._extract_service_from_log_intent(val.lower())
            # If extraction returned nothing (user typed just the bare name), use raw
            if not service:
                service = re.sub(r'\b(last\s+)?\d+\s*(lines?|logs?)?\b', '', val, flags=re.IGNORECASE).strip() or val.strip()
            self.log_request.service = service
            self.log_request.tail    = tail
            self.step = Step.LOGS_ASK_TAIL
            return self.step, None

        # ── LOGS: ask tail ───────────────────────────────────────────────
        elif self.step == Step.LOGS_ASK_TAIL:
            if skip:
                self.log_request.tail = 100
            elif val.isdigit():
                n = int(val)
                if n < 10 or n > 2000:
                    return self.step, "Please enter a number between 10 and 2000. Type 'skip' for default (100)."
                self.log_request.tail = n
            else:
                return self.step, "Please enter a number (e.g. 100, 200). Type 'skip' for default (100)."
            self.step = Step.READY_TO_ANALYZE
            return self.step, None

        return self.step, None

    # ── Log-intent helpers ───────────────────────────────────────────────────

    _LOG_KEYWORDS = re.compile(
        r'\b(log[s]?|analys[ie]s?|analyz[ei]|failing|broken|crash(?:ing)?|errors?|'
        r'why is|check logs?|what.s wrong|debug|diagnos[ei]s?|investigate|'
        r'check|inspect|monitor|trace|troubleshoot|not working|not start(?:ing)?|'
        r'keeps? (crashing|failing|restarting)|is (down|broken|failing)|'
        r'what.s? happening|what.s? going on|what.s? wrong|what.s? up with|'
        r'happening|issue|problem|broken|slow|stuck|hanging|unreachable)\b',
        re.IGNORECASE,
    )

    def _is_log_intent(self, text: str) -> bool:
        return bool(self._LOG_KEYWORDS.search(text))

    # Common service name words to skip when extracting service from intent message
    _LOG_NOISE = re.compile(
        r'\b(log[s]?|analys[ie]s?|analyz[ei]|of|for|the|a|an|last|lines?|'
        r'give me|show me|can you|please|i want|i need|to see|'
        r'why is|what.s wrong|what.s? happening|what.s? going|what.s? up|'
        r'what|going|on|happening|wrong|up|with|about|'
        r'check|debug|diagnos[ei]s?|investigate|inspect|monitor|trace|troubleshoot|'
        r'failing|broken|crash(?:ing)?|errors?|not working|not start(?:ing)?|'
        r'keeps?|is|are|my|service|issue|problem|slow|stuck|hanging)\b',
        re.IGNORECASE,
    )

    # Words that are never a container name — used in the fallback only
    _NOISE_WORDS = {
        'log', 'logs', 'analyze', 'analysis', 'analyses', 'check', 'of', 'for',
        'the', 'a', 'an', 'give', 'me', 'show', 'can', 'you', 'please', 'want',
        'need', 'see', 'why', 'is', 'are', 'what', 'going', 'on', 'happening',
        'wrong', 'up', 'with', 'about', 'debug', 'diagnose', 'investigate',
        'inspect', 'monitor', 'trace', 'failing', 'broken', 'crashing', 'errors',
        'error', 'working', 'starting', 'keeps', 'service', 'issue', 'problem',
        'slow', 'stuck', 'hanging', 'okay', 'ok', 'look', 'last', 'lines', 'line',
        'get', 'fetch', 'pull', 'its', 'it', 'in', 'at', 'do', 'not', 'my', 'your',
    }

    def _extract_service_from_log_intent(self, text: str) -> str:
        """Try to extract a service name from a free-form log intent message."""
        # First: match against actual containers (running + stopped) by exact substring
        try:
            import log_analyzer as _la
            known = _la.list_services()
            text_lower = text.lower()
            # Longest match first so 'my-redis-cache' beats 'redis'
            for svc in sorted(known, key=len, reverse=True):
                if svc.lower() in text_lower:
                    return svc
        except Exception:
            pass
        # Fallback: split on spaces (preserves hyphenated names like "my-omar"),
        # discard pure noise words, keep first valid container-name token.
        tokens = re.sub(r'\b\d+\b', '', text.lower()).split()
        candidates = [
            t for t in tokens
            if re.match(r'^[a-z0-9][a-z0-9_-]*$', t) and t not in self._NOISE_WORDS
        ]
        return candidates[0] if candidates else ""

    def _extract_tail_from_log_intent(self, text: str) -> int:
        """Extract a line count from message like 'last 200 lines'. Default 100."""
        m = re.search(r'\b(\d+)\s*(lines?)?\b', text)
        if m:
            n = int(m.group(1))
            if 10 <= n <= 2000:
                return n
        return 100

    def reset(self):
        self.__init__()
