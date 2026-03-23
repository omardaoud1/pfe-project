# CLAUDE.md

PFE project: autonomous infrastructure remediation system. Docker-based, orchestrated from `monitoring/docker-compose.yml`.

## Run
```bash
cd monitoring && docker compose up -d
```
Rebuild a service: `docker compose build <service> && docker compose up -d <service>`

## Pipeline
Prometheus → Alertmanager → n8n → `POST /decide` → decision-engine:8000 → n8n → `POST /execute` (JWT) → action-executor:8001 → `POST /execution-result` → decision-engine (Redis)

## Services
| Service | Port |
|---|---|
| decision-engine | 8000 |
| action-executor | 8001 |
| docker-watcher | — (runs every 30s) |
| n8n | 5678 |
| Prometheus | 9090 |
| Alertmanager | 9093 |
| Grafana | 3000 |
| redis-history | 6380 |

## Key Files
- `decision-engine/app/rules.py` — `evaluate_rules()`, mutated at runtime by docker-watcher
- `decision-engine/app/confidence.py` — +0.05/success, -0.15/failure over last 5 records
- `decision-engine/app/history.py` — Redis LPUSH/LTRIM, 30 entries max, key=SHA256(`type:service`)
- `action-executor/main.py` — `ACTION_MAP`, mutated at runtime, requires JWT (`ACTION_EXECUTOR_SECRET`)
- `docker-watcher/watcher.py` — auto-discovers containers on `monitoring_default` network

## Safety Levels
- `2` → manual approval in n8n
- `3` → auto-execute if confidence ≥ 0.7

## Add a New Rule
1. `action-executor/main.py` → add to `ACTION_MAP`
2. `decision-engine/app/rules.py` → add rule before fallback comment
3. `monitoring/prometheus/rules/host-alerts.yml` → add alert rule
4. `curl -X POST http://localhost:9090/-/reload`

## Auto-Discovery Labels
```yaml
labels:
  - "monitoring.port=8080"
  - "monitoring.probe=http"
```

## DockerAgent (`agent/`)
Conversational CLI/API for managing Docker Compose services.
- `agent.py` — CLI
- `api.py` — FastAPI on port 8002 (`POST /chat`, `POST /reset`, `GET /health`)
- LLM: Ollama `phi3:mini` via `llm_client.py`
- ADD flow: name→image→port→probe→restart→env→volumes→depends_on→command→confirm→execute
- REMOVE flow: name→confirm→execute

## Env Vars
- `ACTION_EXECUTOR_SECRET` — JWT secret, set in `monitoring/.env`
- `REDIS_URL` — defaults to `redis://redis-history:6379/0`
- `PROMETHEUS_URL` — defaults to `http://prometheus:9090`
