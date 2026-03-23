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
    if incident_type == "DiskUsageHigh":
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
    # Redis History down
    # --------------------
    if incident_type == "RedisHistoryDown" and service == "redis-history":
        return RuleResult(
            action="restart_redis_history",
            action_params={},
            base_confidence=0.9,
            safety_level=3,
            reason="Redis History service is down — auto-restart to restore confidence learning"
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
    # test-agent down (auto-discovered)
    # --------------------
    if incident_type == "TestagentDown" and service == "test-agent":
        return RuleResult(
            action="restart_test_agent",
            action_params={},
            base_confidence=0.3,
            safety_level=2,
            reason="Auto-discovered service — low confidence baseline"
        )

    # --------------------
    # docker-agent down (auto-discovered)
    # --------------------
    if incident_type == "DockeragentDown" and service == "docker-agent":
        return RuleResult(
            action="restart_docker_agent",
            action_params={},
            base_confidence=0.3,
            safety_level=2,
            reason="Auto-discovered service — low confidence baseline"
        )

    # --------------------
    # evolution-postgres down (auto-discovered)
    # --------------------
    if incident_type == "EvolutionpostgresDown" and service == "evolution-postgres":
        return RuleResult(
            action="restart_evolution_postgres",
            action_params={},
            base_confidence=0.3,
            safety_level=2,
            reason="Auto-discovered service — low confidence baseline"
        )

    # --------------------
    # evolution-api down (auto-discovered)
    # --------------------
    if incident_type == "EvolutionapiDown" and service == "evolution-api":
        return RuleResult(
            action="restart_evolution_api",
            action_params={},
            base_confidence=0.3,
            safety_level=2,
            reason="Auto-discovered service — low confidence baseline"
        )

    # --------------------
    # ngrok down (auto-discovered)
    # --------------------
    if incident_type == "NgrokDown" and service == "ngrok":
        return RuleResult(
            action="restart_ngrok",
            action_params={},
            base_confidence=0.3,
            safety_level=2,
            reason="Auto-discovered service — low confidence baseline"
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
