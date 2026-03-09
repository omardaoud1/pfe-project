# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is an **autonomous infrastructure remediation system** — a PFE (final-year project) that automatically detects, decides on, and executes remediation actions for infrastructure incidents. The system is entirely Docker-based and orchestrated from `monitoring/docker-compose.yml`.

## Running the System

All services run together via Docker Compose from the `monitoring/` directory:

```bash
cd monitoring
cp .env.example .env          # then fill in ACTION_EXECUTOR_SECRET
docker compose up -d
```

To rebuild a specific service after code changes:
```bash
docker compose build decision-engine && docker compose up -d decision-engine
docker compose build action-executor && docker compose up -d action-executor
docker compose build docker-watcher  && docker compose up -d docker-watcher
```

To develop `decision-engine` locally (outside Docker):
```bash
cd decision-engine
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cd app
uvicorn app:app --reload --port 8000
```

**Required external Docker volumes** (create once before first `docker compose up`):
```bash
docker volume create n8n_data
docker volume create pfe-monitoring_grafana_data
```

## Architecture

### Alert → Decision → Action Pipeline

```
Prometheus → Alertmanager → n8n (Workflow 1: Alert Intake)
                                 ↓
                            n8n (Workflow 2: Decision Orchestration)
                                 ↓ POST /decide
                            decision-engine:8000
                                 ↓
                            n8n (Workflow 3/4: Execution)
                                 ↓ POST /execute (JWT-authenticated)
                            action-executor:8001
                                 ↓ POST /execution-result
                            decision-engine:8000  (persist to Redis)
```

1. **Prometheus** scrapes metrics and fires alerts (configured in `monitoring/prometheus/`).
2. **Alertmanager** routes all alerts as webhooks to n8n at `/webhook/prod/alerts/intake`.
3. **n8n** (port 5678) orchestrates 4 workflows (JSON definitions in `workflows/`). Workflows are not auto-loaded — import them manually in the n8n UI.
4. **decision-engine** (port 8000) receives `POST /decide` with an `IncidentInput`, applies static rules from `rules.py`, adjusts confidence using Redis history, and returns a `DecisionOutput`.
5. **action-executor** (port 8001) receives `POST /execute` (requires JWT), maps action names to shell commands, and runs them via `subprocess`.
6. After execution, n8n calls `POST /execution-result` on the decision-engine to persist the outcome in Redis (redis-history, port 6380).

### Key Services

| Service | Port | Description |
|---|---|---|
| decision-engine | 8000 | FastAPI — incident → decision logic |
| action-executor | 8001 | FastAPI — executes remediation commands |
| docker-watcher | — | Auto-discovers new containers every 30s |
| n8n | 5678 | Workflow automation / orchestration |
| Prometheus | 9090 | Metrics collection |
| Alertmanager | 9093 | Alert routing |
| Grafana | 3000 | Dashboards |
| redis-history | 6380 | Stores decision history (separate from monitoring Redis) |

### Decision Engine Internals (`decision-engine/app/`)

- **`rules.py`** — Static `evaluate_rules()` function. Maps `(incident_type, service)` pairs to a `RuleResult` (action, base_confidence, safety_level, reason). **This file is mutated at runtime by docker-watcher** to add auto-discovered services.
- **`confidence.py`** — `compute_confidence()` uses the last 5 history records: +0.05 per success, -0.15 per failure (clamped to [0.0, 1.0]).
- **`history.py`** — Redis LPUSH/LTRIM strategy; stores at most 30 entries per `incident_key`. `incident_key` = SHA256(`incident_type:service`).
- **`decision_engine.py`** — Ties it all together: rules → history → confidence → `DecisionOutput`.
- **`models.py`** — Pydantic models: `IncidentInput` and `DecisionOutput`.

### Safety Levels

- `safety_level=2` → requires manual approval in n8n before executing
- `safety_level=3` → auto-executes (confidence ≥ 0.7)

### Action Executor (`action-executor/main.py`)

The `ACTION_MAP` dict maps action string keys to shell commands. **This file is mutated at runtime by docker-watcher.** The `/execute` endpoint requires a JWT Bearer token signed with `ACTION_EXECUTOR_SECRET` (env var, set in `monitoring/.env`).

### Auto-Discovery (`docker-watcher/watcher.py`)

Runs every 30 seconds. Detects new containers on the `monitoring_default` Docker network that are not in `IGNORED_CONTAINERS` or `KNOWN_SERVICES`, then:
1. Appends a rule block to `decision-engine/app/rules.py` (before the fallback marker)
2. Appends an entry to `ACTION_MAP` in `action-executor/main.py`
3. If the container has a `monitoring.port` label: adds a Prometheus blackbox scrape job and an alert rule, then reloads Prometheus via `POST /-/reload`

To add a new service to auto-discovery, add these labels in `docker-compose.yml`:
```yaml
labels:
  - "monitoring.port=8080"
  - "monitoring.probe=http"   # or "tcp"
```

### Prometheus Alert Rules

- **`monitoring/prometheus/rules/host-alerts.yml`** — Hand-written rules for known services (HostDown, DiskUsageHigh, RedisDown, RedisHistoryDown, RabbitMQDown, GatewayDown).
- **`monitoring/prometheus/rules/auto-discovered.yml`** — Generated at runtime by docker-watcher for auto-discovered services.

The `incident_type` naming convention used in Prometheus alert labels must match what `rules.py` expects: e.g., alert name `RedisDown` → `incident_type="RedisDown"` in the n8n payload.

## Adding a New Manual Rule

1. Add an entry to `ACTION_MAP` in `action-executor/main.py`
2. Add a rule block in `decision-engine/app/rules.py` before the fallback comment
3. Add a Prometheus alert rule in `monitoring/prometheus/rules/host-alerts.yml`
4. Reload Prometheus: `curl -X POST http://localhost:9090/-/reload`

## Environment

- `ACTION_EXECUTOR_SECRET` — shared JWT secret between n8n and action-executor. Set in `monitoring/.env`.
- `REDIS_URL` — used by decision-engine, defaults to `redis://redis-history:6379/0`.
- `PROMETHEUS_URL` — used by docker-watcher, defaults to `http://prometheus:9090`.
