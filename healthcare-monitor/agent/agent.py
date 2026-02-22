#!/usr/bin/env python3
"""
Integration Monitor Agent - Deliverable 4

Cross-platform agent (Windows / Linux) that:
  1. Collects CPU, memory, disk, and service status via psutil + OS APIs
  2. Tails configured log files line-by-line
  3. Parses HL7 message and ACK events from log lines
  4. POSTs metrics and HL7 events to the central backend over HTTPS

Install:
  pip install -r requirements.txt

Run (Linux):
  python agent.py --config /etc/ihm-agent/config.yaml

Run (Windows, as service):
  python agent.py --config C:\\ihm-agent\\config.yaml

Config: see config.yaml for full reference.
"""
import argparse
import asyncio
import os
import platform
import signal
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import psutil
import yaml

# ---------------------------------------------------------------------------
# PHI masking (inline - no dependency on backend code)
# ---------------------------------------------------------------------------
import re

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE_RE = re.compile(r"\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
_MRN_RE = re.compile(r"(?i)(MRN|patient[\s_-]?id)\s*[=:]\s*(\d{4,20})")


def _mask_mrn(v: str) -> str:
    return "MRN-***" + (v[-4:] if len(v) >= 4 else "****")


def phi_mask_line(line: str) -> str:
    line = _SSN_RE.sub("***-**-XXXX", line)
    line = _PHONE_RE.sub("***-***-XXXX", line)
    line = _MRN_RE.sub(lambda m: f"{m.group(1)}={_mask_mrn(m.group(2))}", line)
    if "PID|" in line:
        start = line.index("PID|")
        fields = line[start:].split("|")
        # Blank name (idx 5), DOB (idx 7), address (idx 11), phone (idx 13)
        for i in [5, 6, 7, 11, 13, 14, 18, 19]:
            if i < len(fields):
                fields[i] = "***"
        line = line[:start] + "|".join(fields)
    return line


# ---------------------------------------------------------------------------
# Log-based HL7 parser (portable subset of backend log_parser.py)
# ---------------------------------------------------------------------------
PATTERNS = [
    # Raw MSH outbound
    {
        "name": "raw MSH outbound",
        "re": re.compile(
            r"MSH\|[\\^~&]+\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|[^|]*\|[^|]*\|([A-Z]+\^?[A-Z0-9]*)\|([^|\r\n]+)\|",
            re.I,
        ),
        "direction": "outbound",
        "groups": {"sending_app": 1, "sending_fac": 2, "recv_app": 3, "recv_fac": 4,
                   "msg_type": 5, "ctrl_id": 6},
    },
    # Raw MSA (ACK)
    {
        "name": "raw MSA ack",
        "re": re.compile(r"MSA\|([A-Z]{2})\|([^|\r\n]+)", re.I),
        "direction": "inbound_ack",
        "groups": {"ack_code": 1, "ctrl_id": 2},
    },
    # Rhapsody outbound
    {
        "name": "rhapsody outbound",
        "re": re.compile(
            r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\s+\w+\s+\[.*?\]\s+Sent message\s+(\S+)\s+\(([A-Z]+\^?[A-Z0-9]*)\)",
            re.I,
        ),
        "direction": "outbound",
        "groups": {"ts": 1, "ctrl_id": 2, "msg_type": 3},
        "ts_fmt": "%Y/%m/%d %H:%M:%S",
    },
    # Rhapsody ACK
    {
        "name": "rhapsody ack",
        "re": re.compile(
            r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\s+\w+\s+\[.*?\]\s+Received ACK\s+\(([A-Z]{2})\)\s+for\s+(\S+)",
            re.I,
        ),
        "direction": "inbound_ack",
        "groups": {"ts": 1, "ack_code": 2, "ctrl_id": 3},
        "ts_fmt": "%Y/%m/%d %H:%M:%S",
    },
]


def parse_log_line(line: str) -> Optional[Dict]:
    for pat in PATTERNS:
        m = pat["re"].search(line)
        if not m:
            continue
        g = pat["groups"]
        result = {"direction": pat["direction"]}
        for k, idx in g.items():
            try:
                result[k] = m.group(idx)
            except IndexError:
                result[k] = None
        # Split msg_type into type + event
        if "msg_type" in result and result["msg_type"]:
            parts = (result["msg_type"] or "").split("^", 1)
            result["message_type"] = parts[0]
            result["event_type"] = parts[1] if len(parts) > 1 else None
        return result
    return None


# ---------------------------------------------------------------------------
# Service checker
# ---------------------------------------------------------------------------
IS_WINDOWS = platform.system().lower() == "windows"


def get_service_statuses(service_names: List[str]) -> Dict[str, str]:
    statuses: Dict[str, str] = {}
    if IS_WINDOWS:
        _check_windows_services(service_names, statuses)
    else:
        _check_linux_services(service_names, statuses)
    return statuses


def _check_windows_services(names: List[str], out: Dict[str, str]) -> None:
    try:
        import win32service  # pywin32
        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ENUMERATE_SERVICE)
        for name in names:
            try:
                svc = win32service.OpenService(scm, name, win32service.SERVICE_QUERY_STATUS)
                status = win32service.QueryServiceStatus(svc)
                # status[1] = dwCurrentState: 4=running, 1=stopped
                out[name] = "running" if status[1] == 4 else "stopped"
                win32service.CloseServiceHandle(svc)
            except Exception:
                out[name] = "unknown"
        win32service.CloseServiceHandle(scm)
    except ImportError:
        for name in names:
            out[name] = "unknown"


def _check_linux_services(names: List[str], out: Dict[str, str]) -> None:
    for name in names:
        try:
            ret = os.system(f"systemctl is-active --quiet {name} 2>/dev/null")
            out[name] = "running" if ret == 0 else "stopped"
        except Exception:
            out[name] = "unknown"


# ---------------------------------------------------------------------------
# Metrics collection
# ---------------------------------------------------------------------------
def collect_metrics(cfg: Dict) -> Dict:
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk_path = cfg.get("disk_path", "/")
    try:
        disk = psutil.disk_usage(disk_path)
        disk_pct = disk.percent
        disk_used = disk.used / (1024 ** 3)
        disk_total = disk.total / (1024 ** 3)
    except Exception:
        disk_pct, disk_used, disk_total = None, None, None

    load_1m = load_5m = load_15m = None
    if not IS_WINDOWS:
        try:
            load_1m, load_5m, load_15m = psutil.getloadavg()
        except Exception:
            pass

    service_names = cfg.get("services", [])
    services = get_service_statuses(service_names)

    return {
        "server_name": cfg["server_name"],
        "platform": "windows" if IS_WINDOWS else "linux",
        "cpu_percent": round(cpu, 2),
        "memory_percent": round(mem.percent, 2),
        "memory_used_mb": mem.used // (1024 ** 2),
        "memory_total_mb": mem.total // (1024 ** 2),
        "disk_percent": round(disk_pct, 2) if disk_pct is not None else None,
        "disk_used_gb": round(disk_used, 2) if disk_used is not None else None,
        "disk_total_gb": round(disk_total, 2) if disk_total is not None else None,
        "load_avg_1m": round(load_1m, 2) if load_1m is not None else None,
        "load_avg_5m": round(load_5m, 2) if load_5m is not None else None,
        "load_avg_15m": round(load_15m, 2) if load_15m is not None else None,
        "services": services,
        "agent_version": "1.0.0",
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Log tailer
# ---------------------------------------------------------------------------
class LogTailer:
    """
    Tail a log file from its current end, reading new lines as they appear.
    Handles log rotation by detecting inode changes (Linux) or size resets (Windows).
    """

    def __init__(self, path: str, encoding: str = "utf-8", errors: str = "replace"):
        self.path = path
        self.encoding = encoding
        self.errors = errors
        self._file = None
        self._inode = None
        self._pos = 0

    def _open(self) -> None:
        if self._file:
            try:
                self._file.close()
            except Exception:
                pass
        self._file = open(self.path, "r", encoding=self.encoding, errors=self.errors)
        self._file.seek(0, 2)  # seek to end
        self._pos = self._file.tell()
        if not IS_WINDOWS:
            self._inode = os.stat(self.path).st_ino

    def _detect_rotation(self) -> bool:
        if not IS_WINDOWS:
            try:
                current_inode = os.stat(self.path).st_ino
                return current_inode != self._inode
            except OSError:
                return True
        else:
            try:
                size = os.path.getsize(self.path)
                return size < self._pos
            except OSError:
                return True

    def read_new_lines(self) -> List[str]:
        if self._file is None:
            try:
                self._open()
            except OSError:
                return []

        if self._detect_rotation():
            try:
                self._open()
            except OSError:
                self._file = None
                return []

        lines = []
        try:
            self._file.seek(self._pos)
            for line in self._file:
                lines.append(line.rstrip("\r\n"))
            self._pos = self._file.tell()
        except Exception:
            self._file = None

        return lines


# ---------------------------------------------------------------------------
# HTTP push client
# ---------------------------------------------------------------------------
class AgentClient:
    def __init__(self, base_url: str, api_key: str, verify_ssl: bool = True):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        self.ssl = verify_ssl or None  # None = default (verify), False = skip

    async def post(self, path: str, payload: Dict) -> bool:
        url = f"{self.base_url}{path}"
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.post(
                    url, json=payload,
                    ssl=self.ssl,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status in (200, 201):
                        return True
                    text = await resp.text()
                    print(f"[agent] POST {path} failed {resp.status}: {text[:200]}")
                    return False
        except Exception as e:
            print(f"[agent] POST {path} error: {e}")
            return False


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------
class Agent:
    def __init__(self, cfg: Dict):
        self.cfg = cfg
        self.client = AgentClient(
            base_url=cfg["backend_url"],
            api_key=cfg["api_key"],
            verify_ssl=cfg.get("verify_ssl", True),
        )
        self.server_name = cfg["server_name"]
        self.metrics_interval = cfg.get("metrics_interval_seconds", 60)
        self.log_poll_interval = cfg.get("log_poll_interval_seconds", 5)

        # Initialize tailers for each configured log file
        self._tailers: Dict[str, LogTailer] = {}
        for lf in cfg.get("log_files", []):
            path = lf["path"]
            self._tailers[path] = LogTailer(
                path, encoding=lf.get("encoding", "utf-8")
            )

        self._last_metrics_push = 0.0
        self._pending_messages: List[Dict] = []
        self._flow_id = cfg.get("flow_id")  # optional: associate all messages with a flow

    async def run(self) -> None:
        print(f"[agent] Starting on {self.server_name} ({platform.platform()})")
        while True:
            now = time.monotonic()

            # --- Metrics push ---
            if now - self._last_metrics_push >= self.metrics_interval:
                await self._push_metrics()
                self._last_metrics_push = now

            # --- Log tail ---
            await self._poll_logs()

            await asyncio.sleep(self.log_poll_interval)

    async def _push_metrics(self) -> None:
        payload = collect_metrics(self.cfg)
        ok = await self.client.post("/api/metrics/ingest", payload)
        if ok:
            print(f"[agent] metrics pushed: CPU={payload['cpu_percent']}% "
                  f"MEM={payload['memory_percent']}% "
                  f"DISK={payload.get('disk_percent')}%")
        else:
            print("[agent] metrics push FAILED - will retry next interval")

    async def _poll_logs(self) -> None:
        messages_batch: List[Dict] = []
        acks_batch: List[Dict] = []

        for log_path, tailer in self._tailers.items():
            new_lines = tailer.read_new_lines()
            for line in new_lines:
                if not line.strip():
                    continue
                event = parse_log_line(line)
                if not event:
                    continue

                masked_line = phi_mask_line(line)

                if event["direction"] == "outbound":
                    msg_item = {
                        "message_control_id": event.get("ctrl_id"),
                        "message_type": event.get("message_type", "UNKNOWN"),
                        "event_type": event.get("event_type"),
                        "sending_application": event.get("sending_app"),
                        "receiving_application": event.get("recv_app"),
                        "sending_facility": event.get("sending_fac"),
                        "receiving_facility": event.get("recv_fac"),
                        "direction": "outbound",
                        "raw_header_masked": masked_line[:500],
                        "log_source_file": log_path,
                    }
                    if self._flow_id:
                        msg_item["flow_id"] = self._flow_id
                    if msg_item["message_control_id"]:
                        messages_batch.append(msg_item)

                elif event["direction"] == "inbound_ack":
                    ack_item = {
                        "message_control_id": event.get("ctrl_id"),
                        "ack_code": event.get("ack_code", "AA"),
                    }
                    if ack_item["message_control_id"]:
                        acks_batch.append(ack_item)

        # Push in batches
        if messages_batch:
            await self.client.post("/api/hl7/ingest", {
                "server_name": self.server_name,
                "source": "log",
                "messages": messages_batch,
            })
            print(f"[agent] pushed {len(messages_batch)} HL7 message(s)")

        if acks_batch:
            await self.client.post("/api/hl7/ack/ingest", {
                "server_name": self.server_name,
                "source": "log",
                "acks": acks_batch,
            })
            print(f"[agent] pushed {len(acks_batch)} ACK(s)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def load_config(path: str) -> Dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Integration Monitor Agent")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    args = parser.parse_args()

    cfg = load_config(args.config)
    agent = Agent(cfg)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown()))

    async def _shutdown():
        print("[agent] shutting down")
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in tasks:
            t.cancel()

    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
