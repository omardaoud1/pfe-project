from typing import Dict, Any


class RuleResult:
    def __init__(
        self,
        action: str,
        action_params: Dict[str, Any],
        base_confidence: float,
        safety_level: int,
        reason: str
    ):
        self.action = action
        self.action_params = action_params
        self.base_confidence = base_confidence
        self.safety_level = safety_level
        self.reason = reason


def evaluate_rules(incident_type: str, service: str, severity: str) -> RuleResult:
    """
    Static decision rules based on existing monitoring alerts.
    """

    # --------------------
    # Host down
    # --------------------
    if incident_type == "HostDown":
        return RuleResult(
            action="restart_host",
            action_params={},
            base_confidence=0.4,
            safety_level=2,
            reason="Host is not reachable"
        )

    # --------------------
    # Disk usage high
    # --------------------
    if incident_type == "DiskHigh":
        return RuleResult(
            action="cleanup_disk",
            action_params={},
            base_confidence=0.5,
            safety_level=2,
            reason="Disk usage above threshold"
        )

    # --------------------
    # Redis down
    # --------------------
    if incident_type == "RedisDown" and service == "redis":
        return RuleResult(
            action="restart_redis",
            action_params={},
            base_confidence=0.7,
            safety_level=3,
            reason="Redis service is down"
        )

    # --------------------
    # RabbitMQ down
    # --------------------
    if incident_type == "RabbitMQDown" and service == "rabbitmq":
        return RuleResult(
            action="restart_rabbitmq",
            action_params={},
            base_confidence=0.7,
            safety_level=3,
            reason="RabbitMQ service is down"
        )

    # --------------------
    # Gateway down (Blackbox)
    # --------------------
    if incident_type == "GatewayDown":
        return RuleResult(
            action="restart_gateway",
            action_params={},
            base_confidence=0.5,
            safety_level=2,
            reason="Gateway endpoint unreachable (blackbox alert)"
        )

    # --------------------
    # Fallback (unknown incident)
    # --------------------
    return RuleResult(
        action="noop",
        action_params={},
        base_confidence=0.1,
        safety_level=2,
        reason="No matching rule for this incident"
    )

