# Integration Full-Body Monitor

A single-pane-of-glass dashboard for hospital RIS/HIS/Interface Engine environments.

## Quick Start

```bash
# 1. Copy and edit environment
cp .env.example .env
# Edit .env: set SECRET_KEY, POSTGRES_PASSWORD, AGENT_API_KEYS

# 2. Start all services
docker compose up -d

# 3. Open browser
open http://localhost:3000

# Login: admin / ChangeMe123! (CHANGE ON FIRST LOGIN)
```

## Architecture

See ARCHITECTURE.md for full design, DB schema, and API documentation.

## Agent Setup

Install the agent on each server to monitor:

```bash
pip install -r agent/requirements.txt
cp agent/config.yaml /etc/ihm-agent/config.yaml
# Edit config.yaml: set server_name, backend_url, api_key, log_files
python agent/agent.py --config /etc/ihm-agent/config.yaml
```

## Running Unit Tests

```bash
pip install pytest pytest-asyncio aiohttp
pytest tests/backend/ -v
```

## Pages

| Page             | URL         | Purpose                                           |
|------------------|-------------|---------------------------------------------------|
| Overview         | /           | All subsystems at a glance, active BITE alerts    |
| Stuck Messages   | /stuck      | HL7 outbound messages with no ACK, age-sorted     |
| Endpoint Status  | /endpoints  | TCP/HTTP probe status, latency, probe history     |

## PHI Safety

- All PID segments are masked before storage (MRN last-4 retained, name/DOB/SSN blanked)
- Agent applies masking before pushing log lines over the wire
- No full MRN, patient name, or DOB is stored in the database or shown in the UI
- BITE alert text is PHI-free (contains only routing metadata)

## Answers Needed

At the end of setup, provide:
1. Which interface engine are you using? (Rhapsody, Mirth Connect, Ensemble, other)
2. Which servers can you install the agent on?
3. Is HL7 transport MLLP or file-drop for outbound messages?
