"""
Integration tests for the backend API.
Requires a running backend + postgres (docker compose up -d postgres backend).

Run: TEST_BASE_URL=http://localhost:8000 pytest tests/integration/ -v
"""
import os
import pytest
import httpx
import uuid

BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8000")
AGENT_KEY = os.environ.get("AGENT_API_KEY", "CHANGE-ME-AGENT-KEY-1")


@pytest.fixture(scope="module")
def auth_token():
    """Authenticate as admin and return token."""
    r = httpx.post(f"{BASE_URL}/api/auth/login",
                   json={"username": "admin", "password": "ChangeMe123!"})
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json()["access_token"]


@pytest.fixture
def authed(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}


@pytest.fixture
def agent_headers():
    return {"X-API-Key": AGENT_KEY}


class TestHealth:
    def test_health_endpoint(self):
        r = httpx.get(f"{BASE_URL}/health")
        assert r.status_code == 200
        assert r.json()["status"] in ("ok", "degraded")


class TestAuth:
    def test_login_success(self, auth_token):
        assert auth_token and len(auth_token) > 10

    def test_login_bad_creds(self):
        r = httpx.post(f"{BASE_URL}/api/auth/login",
                       json={"username": "admin", "password": "WRONG"})
        assert r.status_code == 401

    def test_me_with_token(self, authed):
        r = httpx.get(f"{BASE_URL}/api/auth/me", headers=authed)
        assert r.status_code == 200
        assert r.json()["username"] == "admin"

    def test_me_without_token(self):
        r = httpx.get(f"{BASE_URL}/api/auth/me")
        assert r.status_code == 401


class TestEndpoints:
    def test_create_and_delete(self, authed):
        # Create
        r = httpx.post(f"{BASE_URL}/api/endpoints", headers=authed,
                       json={"name": "test-ep", "host": "127.0.0.1", "port": 9876, "protocol": "tcp"})
        assert r.status_code == 201
        ep_id = r.json()["id"]

        # Get
        r2 = httpx.get(f"{BASE_URL}/api/endpoints/{ep_id}", headers=authed)
        assert r2.status_code == 200
        assert r2.json()["host"] == "127.0.0.1"

        # List
        r3 = httpx.get(f"{BASE_URL}/api/endpoints", headers=authed)
        assert r3.status_code == 200
        ids = [e["id"] for e in r3.json()]
        assert ep_id in ids

        # Delete
        r4 = httpx.delete(f"{BASE_URL}/api/endpoints/{ep_id}", headers=authed)
        assert r4.status_code == 204


class TestHL7Ingest:
    def test_ingest_message(self, agent_headers):
        ctrl_id = f"TEST-{uuid.uuid4().hex[:8].upper()}"
        r = httpx.post(f"{BASE_URL}/api/hl7/ingest", headers=agent_headers,
                       json={
                           "server_name": "TEST-SERVER",
                           "source": "log",
                           "messages": [{
                               "message_control_id": ctrl_id,
                               "message_type": "ORM",
                               "event_type": "O01",
                               "sending_application": "HIS",
                               "receiving_application": "RIS",
                               "direction": "outbound",
                           }]
                       })
        assert r.status_code == 201
        assert r.json()["ingested"] == 1

    def test_ingest_ack(self, agent_headers):
        ctrl_id = f"ACK-{uuid.uuid4().hex[:8].upper()}"
        # First ingest the message
        httpx.post(f"{BASE_URL}/api/hl7/ingest", headers=agent_headers,
                   json={"server_name": "TEST", "source": "log",
                         "messages": [{"message_control_id": ctrl_id, "message_type": "ORM",
                                       "sending_application": "HIS", "direction": "outbound"}]})
        # Then ACK it
        r = httpx.post(f"{BASE_URL}/api/hl7/ack/ingest", headers=agent_headers,
                       json={"server_name": "TEST", "source": "log",
                             "acks": [{"message_control_id": ctrl_id, "ack_code": "AA"}]})
        assert r.status_code == 201
        assert r.json()["matched"] == 1

    def test_hl7_stats(self, authed):
        r = httpx.get(f"{BASE_URL}/api/hl7/stats", headers=authed)
        assert r.status_code == 200
        assert "pending" in r.json()
        assert "acked" in r.json()


class TestMetricsIngest:
    def test_ingest_metrics(self, agent_headers):
        r = httpx.post(f"{BASE_URL}/api/metrics/ingest", headers=agent_headers,
                       json={
                           "server_name": "TEST-SERVER-01",
                           "platform": "linux",
                           "cpu_percent": 42.5,
                           "memory_percent": 65.0,
                           "memory_used_mb": 4096,
                           "memory_total_mb": 8192,
                           "disk_percent": 55.0,
                           "disk_used_gb": 100.0,
                           "disk_total_gb": 200.0,
                           "services": {"rhapsody": "running"},
                           "agent_version": "1.0.0",
                       })
        assert r.status_code == 201

    def test_server_appears_in_list(self, authed, agent_headers):
        # Ingest first
        httpx.post(f"{BASE_URL}/api/metrics/ingest", headers=agent_headers,
                   json={"server_name": "LIST-TEST-SERVER", "platform": "linux",
                         "cpu_percent": 10.0, "memory_percent": 20.0,
                         "disk_percent": 30.0, "services": {}, "agent_version": "1.0.0"})
        r = httpx.get(f"{BASE_URL}/api/metrics/servers", headers=authed)
        assert r.status_code == 200
        names = [s["server_name"] for s in r.json()]
        assert "LIST-TEST-SERVER" in names


class TestDashboard:
    def test_summary_shape(self, authed):
        r = httpx.get(f"{BASE_URL}/api/dashboard/summary", headers=authed)
        assert r.status_code == 200
        data = r.json()
        assert "endpoints" in data
        assert "hl7" in data
        assert "alerts" in data
        assert "servers" in data
        assert "last_updated" in data


class TestAlerts:
    def test_list_alerts(self, authed):
        r = httpx.get(f"{BASE_URL}/api/alerts", headers=authed)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_active_count(self, authed):
        r = httpx.get(f"{BASE_URL}/api/alerts/active/count", headers=authed)
        assert r.status_code == 200
        data = r.json()
        assert "critical" in data
        assert "warning" in data
