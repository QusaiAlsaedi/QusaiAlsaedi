"""
Log-based HL7 message/ACK parser.

Implements Option B: parse interface engine log files line-by-line,
extract HL7 message events (outbound sends and inbound ACKs)
without needing direct DB or MLLP access.

Used by the agent's log tail loop and by the central backend
when agents push raw log lines.

PHI masking is applied before any data leaves this module.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from app.core.phi_masking import mask_log_line

# ---------------------------------------------------------------------------
# Built-in patterns for common interface engines
# ---------------------------------------------------------------------------

BUILTIN_PATTERNS: List[Dict[str, Any]] = [
    # --- Mirth Connect: outbound message sent ---
    {
        "name": "Mirth outbound sent (raw MSH)",
        "engine_type": "mirth",
        "log_pattern": r"MSH\|[\\^~&]+\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|[^|]*\|[^|]*\|([A-Z]+\^?[A-Z0-9]*)\|([^|\r\n]+)\|",
        "sending_app_group": 1,
        "sending_fac_group": 2,
        "recv_app_group": 3,
        "recv_fac_group": 4,
        "message_type_group": 5,
        "control_id_group": 6,
        "direction_default": "outbound",
        "example_line": "MSH|^~\\&|HIS|HOSP|RIS|RADIOLOGY|20240221140523||ORM^O01|MSG001|P|2.4",
    },
    # --- Mirth Connect: ACK received (MSA line) ---
    {
        "name": "Mirth ACK received (MSA)",
        "engine_type": "mirth",
        "log_pattern": r"MSA\|([A-Z]{2})\|([^|\r\n]+)",
        "ack_code_group": 1,
        "control_id_group": 2,
        "direction_default": "inbound_ack",
        "example_line": "MSA|AA|MSG001|Message accepted",
    },
    # --- Rhapsody: outbound sent ---
    {
        "name": "Rhapsody TCP output sent",
        "engine_type": "rhapsody",
        "log_pattern": (
            r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\s+\w+\s+"
            r"\[.*?\]\s+Sent message\s+(\S+)\s+\(([A-Z]+\^?[A-Z0-9]*)\)"
        ),
        "timestamp_group": 1,
        "timestamp_format": "%Y/%m/%d %H:%M:%S",
        "control_id_group": 2,
        "message_type_group": 3,
        "direction_default": "outbound",
        "example_line": "2024/02/21 14:05:23 INFO  [OutputCommPoint|TCP-RIS] Sent message MSG001 (ORM^O01)",
    },
    # --- Rhapsody: ACK received ---
    {
        "name": "Rhapsody ACK received",
        "engine_type": "rhapsody",
        "log_pattern": (
            r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\s+\w+\s+"
            r"\[.*?\]\s+Received ACK\s+\(([A-Z]{2})\)\s+for\s+(\S+)"
        ),
        "timestamp_group": 1,
        "timestamp_format": "%Y/%m/%d %H:%M:%S",
        "ack_code_group": 2,
        "control_id_group": 3,
        "direction_default": "inbound_ack",
        "example_line": "2024/02/21 14:05:24 INFO  [InputCommPoint|ACK-RIS] Received ACK (AA) for MSG001",
    },
    # --- Ensemble (InterSystems): outbound ---
    {
        "name": "Ensemble outbound operation",
        "engine_type": "ensemble",
        "log_pattern": (
            r"(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})\s+.*?"
            r"Sending HL7 message.*?type=([A-Z]+\^?[A-Z0-9]*)\s+.*?controlId=([^\s,]+)"
        ),
        "timestamp_group": 1,
        "timestamp_format": "%m/%d/%Y %H:%M:%S",
        "message_type_group": 2,
        "control_id_group": 3,
        "direction_default": "outbound",
        "example_line": "02/21/2024 14:05:23  SendOperation: Sending HL7 message type=ORM^O01 controlId=MSG001",
    },
    # --- Generic: ERROR/FATAL level log lines ---
    {
        "name": "Generic error log entry",
        "engine_type": "generic",
        "log_pattern": r"(?i)(ERROR|FATAL|CRITICAL)\b",
        "direction_default": "none",
        "is_error_pattern": True,
        "example_line": "2024-02-21 14:05:23 ERROR Failed to connect to remote host",
    },
]


@dataclass
class ParsedEvent:
    """Result of parsing one log line."""
    event_type: str          # "outbound_message" | "inbound_ack" | "error"
    message_control_id: Optional[str] = None
    message_type: Optional[str] = None        # e.g. "ORM", "ORU"
    message_event: Optional[str] = None       # e.g. "O01"
    sending_application: Optional[str] = None
    sending_facility: Optional[str] = None
    receiving_application: Optional[str] = None
    receiving_facility: Optional[str] = None
    ack_code: Optional[str] = None            # AA | AE | AR
    timestamp: Optional[datetime] = None
    raw_line_masked: str = ""
    log_level: Optional[str] = None


class LogParser:
    """
    Stateful log parser that maintains a pending-message buffer
    to support Option B (log-based) no-ACK detection.
    """

    def __init__(self, patterns: Optional[List[Dict]] = None):
        self._patterns = patterns or BUILTIN_PATTERNS
        self._compiled: List[Dict] = []
        self._compile_patterns()

    def _compile_patterns(self) -> None:
        for pat in self._patterns:
            try:
                compiled = dict(pat)
                compiled["_re"] = re.compile(pat["log_pattern"], re.IGNORECASE)
                self._compiled.append(compiled)
            except re.error as e:
                print(f"[LogParser] Bad pattern '{pat.get('name')}': {e}")

    def parse_line(self, raw_line: str) -> Optional[ParsedEvent]:
        """
        Parse a single raw log line.
        Returns a ParsedEvent if the line matches any pattern, else None.
        PHI masking is applied to raw_line before storage.
        """
        masked_line = mask_log_line(raw_line)

        for pat in self._compiled:
            m = pat["_re"].search(raw_line)
            if not m:
                continue

            # Error pattern
            if pat.get("is_error_pattern"):
                level = m.group(1).upper() if m.lastindex and m.lastindex >= 1 else "ERROR"
                return ParsedEvent(
                    event_type="error",
                    raw_line_masked=masked_line,
                    log_level=level,
                )

            direction = pat.get("direction_default", "outbound")
            ctrl_id = _grp(m, pat.get("control_id_group"))
            msg_type_raw = _grp(m, pat.get("message_type_group"))
            ack_code = _grp(m, pat.get("ack_code_group"))
            ts = _parse_ts(m, pat.get("timestamp_group"), pat.get("timestamp_format"))
            sending_app = _grp(m, pat.get("sending_app_group"))
            recv_app = _grp(m, pat.get("recv_app_group"))

            msg_type, msg_event = _split_type(msg_type_raw)

            if direction == "inbound_ack":
                if not ctrl_id or not ack_code:
                    continue
                return ParsedEvent(
                    event_type="inbound_ack",
                    message_control_id=ctrl_id,
                    ack_code=ack_code,
                    timestamp=ts or datetime.now(timezone.utc),
                    raw_line_masked=masked_line,
                )
            else:
                if not ctrl_id:
                    continue
                return ParsedEvent(
                    event_type="outbound_message",
                    message_control_id=ctrl_id,
                    message_type=msg_type,
                    message_event=msg_event,
                    sending_application=sending_app,
                    receiving_application=recv_app,
                    timestamp=ts or datetime.now(timezone.utc),
                    raw_line_masked=masked_line,
                )

        return None

    def parse_lines(self, lines: List[str]) -> List[ParsedEvent]:
        results = []
        for line in lines:
            event = self.parse_line(line.rstrip("\r\n"))
            if event:
                results.append(event)
        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _grp(m: re.Match, group: Optional[int]) -> Optional[str]:
    if group is None or group == 0:
        return None
    try:
        val = m.group(group)
        return val.strip() if val else None
    except IndexError:
        return None


def _parse_ts(m: re.Match, group: Optional[int], fmt: Optional[str]) -> Optional[datetime]:
    ts_str = _grp(m, group)
    if not ts_str or not fmt:
        return None
    try:
        return datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _split_type(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Split 'ORM^O01' into ('ORM', 'O01')."""
    if not raw:
        return None, None
    parts = raw.split("^", 1)
    return parts[0] or None, (parts[1] if len(parts) > 1 else None)
