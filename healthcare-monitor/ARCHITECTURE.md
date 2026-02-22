# Integration Full-Body Monitor - Architecture & Design

================================================================================
DELIVERABLE 1: ARCHITECTURE DIAGRAM (ASCII)
================================================================================

```
+====================================================================================+
|                    INTEGRATION FULL-BODY MONITOR - DATA FLOW                      |
+====================================================================================+

  MONITORED ENVIRONMENT                 CENTRAL PLATFORM              CONSUMERS
  ===========================           ====================           ==========

  +---------------------+              +-------------------+
  | Interface Engine    |   MLLP/file  |   HL7 Tracker     |
  | (Rhapsody/Mirth/    +---messages-->|   - MSH-10 join   |
  |  Ensemble/Custom)   |<---ACKs------+   - ACK timeout   |
  |                     |              |   - No-ACK detect |
  | /var/log/engine.log +--log tail--->|   - Log parser    |
  +---------------------+              +--------+----------+
                                                |
  +---------------------+              +--------v----------+
  | HIS / RIS App       |              |  Alert Engine     |
  | Servers             |   agent push |  - Reason codes   |         +----------+
  | - CPU/mem/disk      +------------>|  - BITE warnings  +-------->|  React   |
  | - Service status    |              |  - Rule eval      |         |  Web UI  |
  | - App logs          |              +--------+----------+         |          |
  +---------------------+                       |                   | Overview |
                                                |                   | Stuck Msg|
  +---------------------+              +--------v----------+        | Endpoints|
  | Network Endpoints   |   TCP/HTTP   |  Central Poller   |        +----------+
  | host:port pairs     |<---probes----|  (agentless)      |
  | - MLLP listeners    |   reachable? |  - Port checks    |
  | - HTTP healthchecks |   latency?   |  - HTTP probes    |
  | - DB ports          |              +--------+----------+
  +---------------------+                       |
                                                |
                               +----------------v-----------+
                               |      PostgreSQL             |
                               |  endpoints   probes         |
                               |  hl7_messages hl7_acks      |
                               |  server_metrics  alerts      |
                               |  evidence  users  rules      |
                               |  hl7_flows  log_parse_rules  |
                               +----------------------------+

  COMPONENT DETAIL
  ================

  [Central Poller]          - Async TCP connect with timeout -> latency_ms
                            - HTTP GET /health -> status_code
                            - MLLP "keepalive" SYN probe
                            - Runs on configurable interval (default 30s)
                            - Marks endpoint UP/DOWN, writes to probes table

  [HL7 Tracker]             Option A (MLLP): intercept or parse raw MLLP frames
                               MSH-10 -> pending, wait for ACK MSA-2 match
                               timeout = flow.ack_timeout_seconds
                            Option B (Log): regex parse engine log lines
                               extract: timestamp, direction, msg_type, ctrl_id
                               join inbound ACK lines to outbound messages

  [Reason Code Correlator]  When NO_ACK fires, query last 5 minutes of evidence:
                               port_down?  -> NETWORK_FAILURE
                               cpu > 90%?  -> CPU_OVERLOAD
                               disk > 95%? -> DISK_FULL
                               svc stopped?-> ENGINE_DOWN
                               many stuck? -> FLOW_CONGESTION
                               log errors? -> LOG_ERROR_PATTERN
                               else        -> UNKNOWN_DELAY

  [Agent Collector]         Python process on each server (Windows service / systemd unit)
                            Collects: psutil CPU/mem/disk, win32service / systemctl status
                            Tails log files line-by-line, applies regex patterns
                            POSTs to /api/metrics and /api/hl7/ingest over HTTPS
                            Auth: pre-shared API key per server

  [BITE Formatter]          Builds ASCII alert block (see below)
                            Sends to: DB, webhook URL, optional email SMTP

  BITE WARNING FORMAT
  ===================
  +============================================================+
  | BITE WARNING [CRITICAL]  2024-02-21 14:23:44 UTC           |
  +------------------------------------------------------------+
  | WHAT:    ORM^O01 outbound -> RADIOLOGY_RIS                 |
  |          Endpoint: 192.168.1.100:6661                      |
  |          Control IDs: MSG001, MSG002, MSG003 (+44 more)    |
  +------------------------------------------------------------+
  | IMPACT:  47 messages stuck | Oldest age: 18 min 34 sec     |
  +------------------------------------------------------------+
  | EVIDENCE:                                                   |
  |  [FAIL] Port 192.168.1.100:6661 - DOWN since 14:05:10     |
  |  [WARN] CPU on RIS-APP-01 - 94% (5min avg)                |
  |  [OK  ] Disk on RIS-APP-01 - 67%                          |
  |  [UNKN] Engine service status - not monitored              |
  +------------------------------------------------------------+
  | REASON:  NETWORK_FAILURE                                    |
  |          Port unreachable correlates with message halt      |
  +------------------------------------------------------------+
  | ACTION:  1. Check RIS-APP-01 network / NIC                 |
  |          2. Verify MLLP listener on port 6661               |
  |          3. Check interface engine channel status           |
  |          4. Restart channel when port recovers              |
  +============================================================+
```

================================================================================
DELIVERABLE 2: DATABASE SCHEMA
================================================================================

```sql
-- PostgreSQL schema for Integration Full-Body Monitor
-- All PHI fields are masked before storage

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

---------------------------------------------------------------------------
-- Users and authentication
---------------------------------------------------------------------------
CREATE TABLE users (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username     VARCHAR(100) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role         VARCHAR(20) NOT NULL DEFAULT 'viewer', -- admin|operator|viewer
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login   TIMESTAMPTZ
);

CREATE INDEX idx_users_username ON users(username);

---------------------------------------------------------------------------
-- Monitored endpoints (agentless TCP/HTTP probe targets)
---------------------------------------------------------------------------
CREATE TABLE endpoints (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                   VARCHAR(200) NOT NULL,
    host                   VARCHAR(255) NOT NULL,
    port                   INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    protocol               VARCHAR(10) NOT NULL DEFAULT 'tcp', -- tcp|http|https|mllp
    check_interval_seconds INTEGER NOT NULL DEFAULT 30,
    timeout_seconds        INTEGER NOT NULL DEFAULT 5,
    tags                   JSONB NOT NULL DEFAULT '[]',   -- ["ris","mllp","prod"]
    metadata               JSONB NOT NULL DEFAULT '{}',  -- {"env":"prod","app":"RIS"}
    enabled                BOOLEAN NOT NULL DEFAULT TRUE,
    last_status            VARCHAR(20),                  -- up|down|timeout|error
    last_checked_at        TIMESTAMPTZ,
    last_latency_ms        INTEGER,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (host, port)
);

CREATE INDEX idx_endpoints_enabled ON endpoints(enabled);
CREATE INDEX idx_endpoints_last_status ON endpoints(last_status);

---------------------------------------------------------------------------
-- Probe results from agentless poller
---------------------------------------------------------------------------
CREATE TABLE probes (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    endpoint_id   UUID NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    probed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status        VARCHAR(20) NOT NULL, -- up|down|timeout|error
    latency_ms    INTEGER,
    http_status   INTEGER,              -- for http/https probes
    error_message TEXT,
    probe_source  VARCHAR(100)          -- hostname of the poller
);

CREATE INDEX idx_probes_endpoint_id ON probes(endpoint_id);
CREATE INDEX idx_probes_probed_at   ON probes(probed_at DESC);
CREATE INDEX idx_probes_status      ON probes(status);

---------------------------------------------------------------------------
-- Server metrics (agent-based)
---------------------------------------------------------------------------
CREATE TABLE server_metrics (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    server_name      VARCHAR(200) NOT NULL,
    collected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cpu_percent      FLOAT,
    memory_percent   FLOAT,
    memory_used_mb   BIGINT,
    memory_total_mb  BIGINT,
    disk_percent     FLOAT,
    disk_used_gb     FLOAT,
    disk_total_gb    FLOAT,
    load_avg_1m      FLOAT,            -- NULL on Windows
    load_avg_5m      FLOAT,
    load_avg_15m     FLOAT,
    services         JSONB NOT NULL DEFAULT '{}',
    -- e.g. {"rhapsody":"running","mssqlserver":"running","w3svc":"stopped"}
    agent_version    VARCHAR(20),
    platform         VARCHAR(20)       -- windows|linux
);

CREATE INDEX idx_server_metrics_server_name  ON server_metrics(server_name);
CREATE INDEX idx_server_metrics_collected_at ON server_metrics(collected_at DESC);

---------------------------------------------------------------------------
-- HL7 flow configurations
---------------------------------------------------------------------------
CREATE TABLE hl7_flows (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                 VARCHAR(200) NOT NULL,
    sending_application  VARCHAR(100),
    sending_facility     VARCHAR(100),
    receiving_application VARCHAR(100),
    receiving_facility   VARCHAR(100),
    message_types        TEXT[] NOT NULL DEFAULT '{}',   -- {ORM,ORU,ORR}
    ack_timeout_seconds  INTEGER NOT NULL DEFAULT 30,
    transport            VARCHAR(20) NOT NULL DEFAULT 'mllp', -- mllp|file|log
    enabled              BOOLEAN NOT NULL DEFAULT TRUE,
    log_patterns         JSONB NOT NULL DEFAULT '[]',
    -- [{"pattern":"..regex..","ctrl_id_group":2,"type_group":1,"direction":"outbound"}]
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

---------------------------------------------------------------------------
-- HL7 messages tracked (outbound messages awaiting ACK)
---------------------------------------------------------------------------
CREATE TABLE hl7_messages (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_control_id   VARCHAR(200) NOT NULL,         -- MSH-10
    message_type         VARCHAR(20) NOT NULL,           -- ORM|ORU|ORR|ADT
    event_type           VARCHAR(20),                    -- O01|R01|etc
    sending_application  VARCHAR(100),                   -- MSH-3
    sending_facility     VARCHAR(100),                   -- MSH-4
    receiving_application VARCHAR(100),                  -- MSH-5
    receiving_facility   VARCHAR(100),                   -- MSH-6
    message_datetime     TIMESTAMPTZ,                    -- MSH-7 parsed
    detected_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source               VARCHAR(20) NOT NULL,           -- mllp|file|log|agent
    direction            VARCHAR(10) NOT NULL DEFAULT 'outbound',
    raw_header_masked    TEXT,                           -- PHI-masked MSH segment
    patient_id_masked    VARCHAR(100),                   -- masked MRN e.g. MRN-***7892
    status               VARCHAR(20) NOT NULL DEFAULT 'pending',
    -- pending|acked|nacked|timeout|error
    ack_received_at      TIMESTAMPTZ,
    ack_latency_ms       INTEGER,
    flow_id              UUID REFERENCES hl7_flows(id),
    log_source_file      VARCHAR(500),
    log_line_number      INTEGER,
    server_name          VARCHAR(200),
    UNIQUE (message_control_id, sending_application, sending_facility)
);

CREATE INDEX idx_hl7_messages_status       ON hl7_messages(status);
CREATE INDEX idx_hl7_messages_detected_at  ON hl7_messages(detected_at DESC);
CREATE INDEX idx_hl7_messages_ctrl_id      ON hl7_messages(message_control_id);
CREATE INDEX idx_hl7_messages_type         ON hl7_messages(message_type);
CREATE INDEX idx_hl7_messages_flow         ON hl7_messages(flow_id);

---------------------------------------------------------------------------
-- HL7 ACKs received
---------------------------------------------------------------------------
CREATE TABLE hl7_acks (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_control_id VARCHAR(200) NOT NULL, -- MSA-2 -> links to hl7_messages
    ack_code           VARCHAR(10) NOT NULL,   -- AA|AE|AR
    ack_datetime       TIMESTAMPTZ,
    detected_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source             VARCHAR(20),
    error_condition    TEXT,                   -- MSA-3 masked
    raw_header_masked  TEXT,
    server_name        VARCHAR(200)
);

CREATE INDEX idx_hl7_acks_ctrl_id     ON hl7_acks(message_control_id);
CREATE INDEX idx_hl7_acks_detected_at ON hl7_acks(detected_at DESC);

---------------------------------------------------------------------------
-- Alerts
---------------------------------------------------------------------------
CREATE TABLE alerts (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_type       VARCHAR(50) NOT NULL,
    -- PORT_DOWN|NO_ACK_TIMEOUT|HIGH_CPU|HIGH_DISK|SERVICE_DOWN|NACK_RECEIVED
    severity         VARCHAR(20) NOT NULL,    -- critical|warning|info
    title            TEXT NOT NULL,
    description      TEXT,
    reason_code      VARCHAR(50),
    -- NETWORK_FAILURE|CPU_OVERLOAD|DISK_FULL|ENGINE_DOWN|FLOW_CONGESTION|
    -- LOG_ERROR_PATTERN|UNKNOWN_DELAY
    bite_text        TEXT,                    -- full ASCII BITE block
    status           VARCHAR(20) NOT NULL DEFAULT 'open',  -- open|acked|resolved
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acknowledged_at  TIMESTAMPTZ,
    resolved_at      TIMESTAMPTZ,
    acknowledged_by  UUID REFERENCES users(id),
    resolved_by      UUID REFERENCES users(id),
    target_type      VARCHAR(50),             -- endpoint|server|hl7_flow
    target_id        TEXT,                    -- UUID or name
    metadata         JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_alerts_status     ON alerts(status);
CREATE INDEX idx_alerts_severity   ON alerts(severity);
CREATE INDEX idx_alerts_created_at ON alerts(created_at DESC);
CREATE INDEX idx_alerts_type       ON alerts(alert_type);

---------------------------------------------------------------------------
-- Evidence attached to alerts
---------------------------------------------------------------------------
CREATE TABLE evidence (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id       UUID NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    evidence_type  VARCHAR(50) NOT NULL,
    -- port_probe|cpu_spike|disk_usage|service_status|no_ack|log_entry
    collected_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    data           JSONB NOT NULL,
    description    TEXT
);

CREATE INDEX idx_evidence_alert_id ON evidence(alert_id);

---------------------------------------------------------------------------
-- Alert rules
---------------------------------------------------------------------------
CREATE TABLE alert_rules (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             VARCHAR(200) NOT NULL,
    rule_type        VARCHAR(50) NOT NULL,
    -- port_down|no_ack_timeout|high_cpu|high_disk|service_down|nack_rate
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    threshold_value  FLOAT,
    threshold_unit   VARCHAR(20),             -- percent|ms|count|seconds
    window_seconds   INTEGER NOT NULL DEFAULT 300,
    severity         VARCHAR(20) NOT NULL DEFAULT 'warning',
    config           JSONB NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

---------------------------------------------------------------------------
-- Log parsing rules
---------------------------------------------------------------------------
CREATE TABLE log_parse_rules (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                 VARCHAR(200) NOT NULL,
    engine_type          VARCHAR(50),          -- rhapsody|mirth|ensemble|generic
    log_pattern          TEXT NOT NULL,        -- Python regex
    message_type_group   INTEGER,
    control_id_group     INTEGER,
    ack_code_group       INTEGER,
    direction_group      INTEGER,
    direction_default    VARCHAR(10),          -- inbound|outbound
    timestamp_group      INTEGER,
    timestamp_format     VARCHAR(50),
    enabled              BOOLEAN NOT NULL DEFAULT TRUE,
    example_line         TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

---------------------------------------------------------------------------
-- Seed default alert rules
---------------------------------------------------------------------------
INSERT INTO alert_rules (name, rule_type, threshold_value, threshold_unit, severity, config)
VALUES
  ('Port Down', 'port_down', 1, 'count', 'critical',
   '{"consecutive_failures": 2}'),
  ('No ACK Timeout - ORM', 'no_ack_timeout', 60, 'seconds', 'critical',
   '{"message_types": ["ORM"], "min_stuck_count": 1}'),
  ('No ACK Timeout - ORU', 'no_ack_timeout', 120, 'seconds', 'warning',
   '{"message_types": ["ORU"], "min_stuck_count": 3}'),
  ('High CPU Warning', 'high_cpu', 85.0, 'percent', 'warning',
   '{"duration_seconds": 120}'),
  ('High CPU Critical', 'high_cpu', 95.0, 'percent', 'critical',
   '{"duration_seconds": 60}'),
  ('High Disk Warning', 'high_disk', 85.0, 'percent', 'warning', '{}'),
  ('High Disk Critical', 'high_disk', 95.0, 'percent', 'critical', '{}'),
  ('Service Down', 'service_down', 1, 'count', 'critical',
   '{"services": ["rhapsody", "mirth", "ensemble", "mssqlserver"]}'),
  ('NACK Rate High', 'nack_rate', 10.0, 'percent', 'warning',
   '{"window_minutes": 5}');
```

================================================================================
DELIVERABLE 3: BACKEND API DESIGN
================================================================================

```
BASE URL: https://monitor.hospital.local/api

AUTHENTICATION
--------------
POST   /auth/login           { username, password } -> { access_token, role }
POST   /auth/refresh         { refresh_token }       -> { access_token }
POST   /auth/logout
GET    /auth/me                                      -> { id, username, role }

ENDPOINTS (agentless TCP/HTTP targets)
---------------------------------------
GET    /endpoints             ?tags=&status=&page=   -> [Endpoint]
POST   /endpoints             EndpointCreate          -> Endpoint
GET    /endpoints/{id}                               -> Endpoint
PUT    /endpoints/{id}        EndpointUpdate          -> Endpoint
DELETE /endpoints/{id}
GET    /endpoints/{id}/probes ?hours=1               -> [Probe]
GET    /endpoints/{id}/history ?hours=24 -> { uptime_pct, avg_latency_ms, ... }

PROBES (live + historical)
--------------------------
GET    /probes/latest         ?limit=100              -> [Probe+EndpointName]
POST   /probes/trigger        { endpoint_ids: [...] } -> 202 Accepted
GET    /probes/summary                               -> { up:N, down:N, ... }

SERVER METRICS
--------------
POST   /metrics/ingest        AgentMetricsPayload    -> 201  (agent writes here)
GET    /metrics/servers       ?page=                 -> [ServerSummary]
GET    /metrics/servers/{name}                       -> ServerDetail
GET    /metrics/servers/{name}/history ?hours=4      -> [ServerMetric]

HL7
---
POST   /hl7/ingest            HL7IngestPayload       -> 201  (agent/MLLP proxy)
GET    /hl7/messages          ?status=&type=&hours=  -> [HL7Message]
GET    /hl7/messages/{id}                            -> HL7MessageDetail
GET    /hl7/stuck             ?min_age_seconds=30    -> [StuckMessage]
GET    /hl7/flows                                    -> [HL7Flow]
POST   /hl7/flows             HL7FlowCreate          -> HL7Flow
PUT    /hl7/flows/{id}
POST   /hl7/ack/ingest        HL7AckPayload          -> 201
GET    /hl7/stats             ?hours=1               -> HL7Stats
GET    /hl7/rules                                    -> [LogParseRule]
POST   /hl7/rules             LogParseRuleCreate     -> LogParseRule
PUT    /hl7/rules/{id}

ALERTS
------
GET    /alerts                ?status=&severity=&type= -> [Alert]
GET    /alerts/{id}                                  -> AlertDetail (includes evidence)
POST   /alerts/{id}/acknowledge
POST   /alerts/{id}/resolve
DELETE /alerts/{id}           (admin only)
GET    /alerts/active/count                          -> { critical: N, warning: N }
GET    /alerts/bite/{id}                             -> { bite_text: "ASCII block" }

DASHBOARD
---------
GET    /dashboard/summary                            -> DashboardSummary
  {
    endpoints:    { total, up, down, degraded },
    servers:      { total, healthy, warning, critical },
    hl7:          { pending, stuck, acked_1h, nacked_1h, timeout_1h },
    alerts:       { open_critical, open_warning, open_info },
    last_updated: ISO8601
  }
GET    /dashboard/timeline    ?hours=4               -> TimelineData

WEBSOCKET
---------
WS     /ws/alerts             (push new alerts in real-time)
WS     /ws/dashboard          (push dashboard summary every 10s)

KEY PAYLOADS
------------
AgentMetricsPayload {
  server_name: str, api_key: str, platform: "windows|linux",
  cpu_percent: float, memory_percent: float, memory_used_mb: int,
  memory_total_mb: int, disk_percent: float, disk_used_gb: float,
  disk_total_gb: float, load_avg_1m: float|null, load_avg_5m: float|null,
  load_avg_15m: float|null, services: dict[str,str],
  agent_version: str, collected_at: ISO8601
}

HL7IngestPayload {
  server_name: str, api_key: str, source: "mllp|file|log|agent",
  messages: [{
    message_control_id: str, message_type: str, event_type: str|null,
    sending_application: str, sending_facility: str,
    receiving_application: str, receiving_facility: str,
    message_datetime: ISO8601|null, direction: "outbound|inbound",
    raw_header_masked: str|null, patient_id_masked: str|null,
    log_source_file: str|null, log_line_number: int|null
  }]
}

HL7AckPayload {
  server_name: str, api_key: str, source: str,
  acks: [{
    message_control_id: str, ack_code: "AA|AE|AR",
    ack_datetime: ISO8601|null, error_condition: str|null
  }]
}

StuckMessage {
  id: UUID, message_control_id: str, message_type: str,
  sending_application: str, receiving_application: str,
  detected_at: ISO8601, age_seconds: int, age_human: str,
  server_name: str, flow_name: str|null,
  reason_code: str|null, reason_label: str|null
}
```

================================================================================
DELIVERABLE 5: HL7 NO-ACK DETECTION DESIGN
================================================================================

```
OPTION A: MSH-10 JOIN (MLLP or file-drop transport)
====================================================

Flow:
  1. Agent (or MLLP proxy) parses outbound HL7 message frame
  2. Extracts MSH.10 (Message Control ID), MSH.3/4/5/6, MSH.9 (type)
  3. POSTs to /hl7/ingest with direction=outbound, status=pending
  4. Tracker background task runs every 15s:
       SELECT * FROM hl7_messages
       WHERE status = 'pending'
         AND direction = 'outbound'
         AND detected_at < NOW() - INTERVAL '(flow.ack_timeout_seconds) seconds'
  5. When ACK arrives (agent parses inbound ACK frame):
       MSA.1 = ack_code (AA/AE/AR)
       MSA.2 = original_message_control_id -> JOIN key
     POSTs to /hl7/ack/ingest
  6. Tracker joins: UPDATE hl7_messages SET status=acked, ack_received_at=...
     WHERE message_control_id = MSA.2

OPTION B: LOG-BASED PARSING (no DB access to engine)
=====================================================

Works by tailing the interface engine's log file.
Each engine has different log formats; we use configurable regex patterns.

REGEX EXAMPLES:

--- Mirth Connect ---
Log line (sent):
  [2024-02-21 14:05:23,447] INFO  (ORM-outbound): Message sent to destination.
  MSH|^~\&|HIS|HOSP|RIS|RADIOLOGY|20240221140523||ORM^O01|MSG_20240221_001234|P|2.4

Pattern A (Mirth outbound):
  PATTERN: r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\] INFO.*Message sent.*\n.*MSH\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|\|([A-Z]+\^[A-Z0-9]+)\|([^\|]+)\|'
  GROUPS: 1=timestamp, 2=msg_type^event, 3=ctrl_id
  DIRECTION: outbound

Log line (ACK received):
  [2024-02-21 14:05:24,112] INFO  (ORM-outbound): Message response received. (AA)
  MSH|^~\&|RIS|RADIOLOGY|HIS|HOSP|20240221140524||ACK^O01|ACK_001234|P|2.4
  MSA|AA|MSG_20240221_001234|Message accepted

Pattern B (Mirth ACK):
  PATTERN: r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\] INFO.*response received.*\(([A-Z]{2})\)'
  GROUPS: 1=timestamp, 2=ack_code
  Note: pair with next MSA line to get ctrl_id

--- Rhapsody ---
Log line (sent):
  2024/02/21 14:05:23 INFO  [OutputCommPoint|TCP-to-RIS] Sent message MSG_20240221_001234 (ORM^O01) to 192.168.1.100:6661

Pattern C (Rhapsody TCP output):
  PATTERN: r'(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \w+\s+\[.*?\] Sent message ([^\s]+) \(([A-Z]+\^[A-Z0-9]+)\)'
  GROUPS: 1=timestamp, 2=ctrl_id, 3=msg_type^event
  DIRECTION: outbound

Log line (ACK):
  2024/02/21 14:05:24 INFO  [InputCommPoint|TCP-ACK-from-RIS] Received ACK (AA) for MSG_20240221_001234

Pattern D (Rhapsody ACK):
  PATTERN: r'(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \w+\s+\[.*?\] Received ACK \(([A-Z]{2})\) for ([^\s]+)'
  GROUPS: 1=timestamp, 2=ack_code, 3=ctrl_id

--- Generic HL7 (raw MSH parsing) ---
Pattern E (raw MSH line in log):
  PATTERN: r'MSH\|[\\^~&]+\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|[^|]*\|[^|]*\|([A-Z]+\^?[A-Z0-9]*)\|([^|]+)\|'
  GROUPS: 1=sending_app, 2=sending_fac, 3=recv_app, 4=recv_fac, 5=msg_type, 6=ctrl_id

Pattern F (raw MSA line for ACK):
  PATTERN: r'MSA\|([A-Z]{2})\|([^|^\r\n]+)'
  GROUPS: 1=ack_code, 2=original_ctrl_id

JOIN LOGIC (Option B):
  pending_log = {ctrl_id -> {timestamp, type, direction}}
  On each log line:
    - If matches outbound pattern: pending_log[ctrl_id] = {now(), type}
    - If matches ACK pattern:
        orig = pending_log.pop(ctrl_id, None)
        if orig: calculate latency, mark acked
        else: record orphan ACK (harmless)

  Background sweep every 30s:
    for ctrl_id, info in pending_log.items():
      if (now - info.timestamp) > ack_timeout:
        emit NO_ACK event -> /hl7/ingest with status=timeout
```

================================================================================
DELIVERABLE 6: ALERT RULES AND REASON CODE LOGIC
================================================================================

```
REASON CODE CORRELATION MATRIX
===============================

Trigger: NO_ACK_TIMEOUT for a message/flow
  Query window: 5 minutes before first stuck message detected

  Evidence Check 1 - Port Status:
    SELECT status FROM probes
    WHERE endpoint_id = (SELECT id FROM endpoints WHERE host=recv_host AND port=recv_port)
      AND probed_at > NOW() - INTERVAL '5 minutes'
    ORDER BY probed_at DESC LIMIT 5;
    -> If 3+ of 5 = 'down':   reason = NETWORK_FAILURE
       action = "Check network path and MLLP listener on {host}:{port}"

  Evidence Check 2 - CPU:
    SELECT AVG(cpu_percent) FROM server_metrics
    WHERE server_name = recv_server
      AND collected_at > NOW() - INTERVAL '5 minutes';
    -> If avg > 90:            reason = CPU_OVERLOAD
       action = "Check process list on {server}. Engine may be thread-starved."

  Evidence Check 3 - Disk:
    SELECT MAX(disk_percent) FROM server_metrics
    WHERE server_name = recv_server
      AND collected_at > NOW() - INTERVAL '5 minutes';
    -> If max > 95:            reason = DISK_FULL
       action = "Free disk space on {server}. Engine may fail to write ACK log."

  Evidence Check 4 - Services:
    SELECT services FROM server_metrics
    WHERE server_name = recv_server
    ORDER BY collected_at DESC LIMIT 1;
    -> If engine_service in ['stopped','failed']:  reason = ENGINE_DOWN
       action = "Restart {service} on {server}. All queued messages will re-send."

  Evidence Check 5 - Flow Congestion:
    SELECT COUNT(*) FROM hl7_messages
    WHERE status = 'pending'
      AND flow_id = current_flow_id
      AND detected_at > NOW() - INTERVAL '10 minutes';
    -> If count > 20:          reason = FLOW_CONGESTION
       action = "Flow backlog detected. Check engine queue depth and throughput."

  Evidence Check 6 - Log errors:
    (from agent log tail, stored in evidence table)
    SELECT COUNT(*) FROM evidence
    WHERE evidence_type = 'log_entry'
      AND (data->>'level') IN ('ERROR','FATAL','CRITICAL')
      AND collected_at > NOW() - INTERVAL '5 minutes'
      AND (data->>'server_name') = recv_server;
    -> If count > 0:           reason = LOG_ERROR_PATTERN
       action = "Check engine error log on {server}. Pattern: {log_excerpt}"

  Default: reason = UNKNOWN_DELAY
    action = "Manually check {engine} channel for {flow_name}."

SEVERITY ESCALATION
===================
  NO_ACK:
    age  0-60s  -> info  (may still arrive)
    age 60-300s -> warning
    age  >300s  -> critical
    stuck_count > 10 -> escalate one level

  PORT_DOWN:
    1 failure   -> warning
    2+ failures -> critical (raise immediately)

  CPU:
    85-94%  -> warning
    95%+    -> critical

  DISK:
    85-94%  -> warning
    95%+    -> critical

DE-DUPLICATION
==============
  Before creating alert:
    SELECT id FROM alerts
    WHERE alert_type = :type
      AND target_id = :target_id
      AND status = 'open'
      AND created_at > NOW() - INTERVAL '30 minutes'
    LIMIT 1;
  -> If found: update metadata / evidence only, do NOT duplicate
```
