# Integration Monitor - UAT Scenarios
# Deliverable 8: Testing Plan

================================================================================
TESTING PLAN: UNIT + INTEGRATION + UAT
================================================================================

## Unit Tests (pytest, no DB required)

### Location: tests/backend/

| Test File               | What is Tested                                      | Coverage Goal |
|-------------------------|-----------------------------------------------------|---------------|
| test_phi_masking.py     | PID segment masking, MRN truncation, log line mask  | 100%          |
| test_log_parser.py      | Rhapsody/Mirth/raw MSH patterns, ACK join logic     | 95%           |
| test_reason_codes.py    | Priority matrix, BITE ASCII output, confidence      | 90%           |
| test_poller.py          | TCP probe up/down/timeout, HTTP 200/500             | 85%           |

Run all unit tests:
  cd healthcare-monitor
  pip install pytest pytest-asyncio aiohttp
  pytest tests/backend/ -v

================================================================================
## Integration Tests (pytest + docker compose)
================================================================================

### Prerequisites:
  docker compose up -d postgres backend
  export TEST_BASE_URL=http://localhost:8000

### Test: Authentication flow
  1. POST /api/auth/login  { username: "admin", password: "ChangeMe123!" }
     EXPECT: 200 + access_token
  2. POST /api/auth/login  { username: "admin", password: "wrong" }
     EXPECT: 401
  3. GET /api/auth/me (with token)
     EXPECT: 200 + { role: "admin" }

### Test: Endpoint CRUD + Probe cycle
  1. POST /api/endpoints  { name: "test", host: "127.0.0.1", port: 8000, protocol: "tcp" }
     EXPECT: 201 + id
  2. GET /api/endpoints/{id}
     EXPECT: 200 + correct data
  3. Wait 35 seconds for probe cycle
  4. GET /api/endpoints/{id}
     EXPECT: last_status = "up" (since backend is running)
  5. GET /api/endpoints/{id}/probes
     EXPECT: >= 1 probe with status "up"
  6. DELETE /api/endpoints/{id}
     EXPECT: 204

### Test: HL7 No-ACK detection (Option B via /ingest)
  1. POST /api/hl7/ingest (with agent API key) with 1 ORM message, source="log"
     EXPECT: 201, ingested: 1
  2. GET /api/hl7/messages?status=pending
     EXPECT: 1 message returned
  3. Wait ACK_TIMEOUT_SECONDS (default 60s) for tracker to mark as timeout
  4. GET /api/hl7/stuck?min_age_seconds=30
     EXPECT: message appears in stuck list
  5. GET /api/alerts?type=NO_ACK_TIMEOUT
     EXPECT: 1 open alert (alert engine fired within 30s of tracker marking timeout)
  6. GET /api/alerts/{id}
     EXPECT: bite_text non-null, reason_code = "UNKNOWN_DELAY" (no correlated evidence)
  7. POST /api/hl7/ack/ingest with ack_code="AA" for the same ctrl_id
     EXPECT: 201, matched: 1
  8. GET /api/hl7/messages?status=acked
     EXPECT: the message is now "acked"

### Test: Agent metrics ingest
  1. POST /api/metrics/ingest (with agent key) with cpu_percent=96
     EXPECT: 201
  2. GET /api/metrics/servers
     EXPECT: server appears with status "critical"
  3. GET /api/alerts?type=HIGH_CPU
     EXPECT: 1 critical alert fired

### Test: Dashboard summary
  1. GET /api/dashboard/summary
     EXPECT: endpoints.total >= 0, hl7.stuck_30s >= 0

================================================================================
## UAT Scenarios (with real interface engine or log simulator)
================================================================================

### UAT-01: Happy Path - ORM Sent and ACKed
  SETUP: Agent running on a server with Rhapsody/Mirth log tailing
  STEPS:
    1. Trigger an ORM^O01 send from the HIS to RIS
    2. Observe the message appearing on the Stuck Messages page (< 5 seconds)
    3. Wait for the RIS to send AA ACK
    4. Observe the message disappears from Stuck Messages page
    5. Check HL7 Stats: acked_1h increments
  PASS CRITERIA:
    - Message visible within 5s of log entry
    - Disappears within 5s of ACK log entry
    - No spurious alert generated

### UAT-02: Port Down Alert (BITE)
  SETUP: Two endpoints configured - HIS MLLP (6661) and RIS MLLP (6661)
  STEPS:
    1. Stop the MLLP listener on the RIS server (or block port on firewall)
    2. Wait 35 seconds (2 probe cycles)
    3. On Overview page: endpoint turns RED "DOWN"
    4. A PORT_DOWN CRITICAL alert appears in the alert banner
    5. Click "VIEW BITE" on the alert
  PASS CRITERIA:
    - BITE text contains: host, port, "NETWORK_FAILURE", action steps
    - ASCII-only output (paste into Notepad/SMS and verify)
    - Alert created within 90 seconds of port going down
    - No PHI in BITE text

### UAT-03: No-ACK with Root Cause Correlation (CPU Overload)
  SETUP: Interface engine configured, agent running, cpu stress tool available
  STEPS:
    1. Start a CPU stress test on the RIS server (e.g., stress --cpu 8)
    2. Send 5 ORM messages from HIS to RIS
    3. RIS is too slow to ACK; wait 65 seconds
    4. Check Stuck Messages page: all 5 appear
    5. Check Alerts: NO_ACK_TIMEOUT fires with reason_code = CPU_OVERLOAD
    6. View BITE: action includes "check process list"
  PASS CRITERIA:
    - reason_code = "CPU_OVERLOAD" (not UNKNOWN_DELAY)
    - BITE mentions server name and CPU percentage
    - confidence = "medium" or "high"

### UAT-04: PHI Safety - Log Ingestion
  SETUP: Agent configured to tail a log file containing real HL7 messages with PID
  STEPS:
    1. Inject a log line: MSH|...|ORM^O01|CTRL001| + PID|1||9998887777^^^HIS^MRN||DOE^JANE|||19850101|
    2. Check backend DB: SELECT raw_header_masked FROM hl7_messages WHERE message_control_id='CTRL001'
  PASS CRITERIA:
    - raw_header_masked does NOT contain "DOE" or "JANE"
    - raw_header_masked does NOT contain "9998887777"
    - raw_header_masked DOES contain "MRN-***7777" (last 4 preserved)
    - raw_header_masked DOES contain "****-**-**" for DOB field
    - No full MRN visible anywhere in the UI or DB

### UAT-05: RBAC - Viewer Cannot Acknowledge
  STEPS:
    1. Login as "viewer" user (role: viewer)
    2. Navigate to an open critical alert
    3. Attempt to click "ACK"
  PASS CRITERIA:
    - API returns 403 Forbidden
    - UI shows error message
    - Alert status remains "open"

### UAT-06: Log Rotation Handling
  SETUP: Agent running with log tail on a file that rotates daily
  STEPS:
    1. Rotate the log file (mv mirth.log mirth.log.1; touch mirth.log)
    2. Write new log entries to the new mirth.log
  PASS CRITERIA:
    - Agent detects rotation (inode change on Linux, size reset on Windows)
    - New log entries are parsed and pushed within 10 seconds
    - No duplicate entries from the rotated file

### UAT-07: Dashboard Auto-Refresh
  STEPS:
    1. Open Overview page in browser
    2. Trigger a port-down condition
    3. Do NOT manually refresh
  PASS CRITERIA:
    - Dashboard shows updated status within 35 seconds (auto-refresh is 30s)
    - Alert banner updates automatically
    - No page reload required

### UAT-08: Stuck Message Age Escalation
  STEPS:
    1. Block port to RIS, send 1 ORM message
    2. Check Stuck Messages at t=45s: severity = WARNING (orange row)
    3. Wait until t=310s: severity should escalate to CRITICAL (red row + new alert)
  PASS CRITERIA:
    - Row background color changes from orange to red at 300s threshold
    - If original alert was WARNING, a new CRITICAL alert fires or severity updates

================================================================================
## Performance / Load Considerations
================================================================================

- Poller: 100 endpoints x 30s interval = ~3.3 probes/sec -> well within async capacity
- HL7 tracker: 10,000 pending messages sweep in < 1 second (indexed query)
- Alert engine: runs every 30s, < 100ms for typical load
- Agent: 1 push/minute per server, trivial overhead
- DB retention: enable pg_cron for cleanup (probes: 7d, metrics: 14d, hl7: 30d)
