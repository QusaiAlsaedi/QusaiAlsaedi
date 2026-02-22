"""
Reason code correlation engine.

Given a NO_ACK_TIMEOUT or FLOW_CONGESTION event, queries the last
CORRELATION_WINDOW_SECONDS of evidence and assigns a reason code
plus a plain-English first-action recommendation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Probe, ServerMetric, Alert, Evidence, HL7Message

REASON_CODES = {
    "NETWORK_FAILURE":    "Network path or MLLP listener is unreachable",
    "CPU_OVERLOAD":       "Destination server CPU is saturated",
    "DISK_FULL":          "Destination server disk is critically full",
    "ENGINE_DOWN":        "Interface engine service is stopped or failed",
    "FLOW_CONGESTION":    "Message backlog building - flow throughput degraded",
    "LOG_ERROR_PATTERN":  "Error-level entries detected in engine logs",
    "UNKNOWN_DELAY":      "No correlated root cause found - manual review needed",
}

ACTION_MAP = {
    "NETWORK_FAILURE": (
        "1. Ping {host} from the interface engine server\n"
        "2. Verify MLLP listener is running on {host}:{port}\n"
        "3. Check firewall rules between engine and {host}:{port}\n"
        "4. Review network switch / VLAN config"
    ),
    "CPU_OVERLOAD": (
        "1. SSH to {server} and run 'top' or 'tasklist'\n"
        "2. Identify the high-CPU process (engine, DB, antivirus scan?)\n"
        "3. If engine process: check thread pool and queue settings\n"
        "4. If OS process: consider scheduling/deferring non-critical jobs"
    ),
    "DISK_FULL": (
        "1. Check disk on {server}: 'df -h' (Linux) or 'Get-PSDrive' (Windows)\n"
        "2. Clear engine log rotation / archive old journals\n"
        "3. Free space to allow engine to write ACK and spool files\n"
        "4. Alert storage team for long-term remediation"
    ),
    "ENGINE_DOWN": (
        "1. SSH to {server} and check service: 'systemctl status {service}'\n"
        "2. Review engine crash log for root cause before restart\n"
        "3. Restart the engine service if safe to do so\n"
        "4. Verify all channels reconnect after restart"
    ),
    "FLOW_CONGESTION": (
        "1. Check engine channel queue depth for {flow}\n"
        "2. Look for slow downstream processing at {receiving_app}\n"
        "3. Temporarily increase thread count on the send channel\n"
        "4. Review message routing for bottlenecks"
    ),
    "LOG_ERROR_PATTERN": (
        "1. Open engine error log on {server}\n"
        "2. Look for exception stack traces near the stuck message times\n"
        "3. Common causes: parse error, connection refused, encoding issue\n"
        "4. Fix and restart affected channel"
    ),
    "UNKNOWN_DELAY": (
        "1. Manually check {flow} channel in engine admin UI\n"
        "2. Review engine logs around {detected_at}\n"
        "3. Verify ACK settings match sender expectations (timeout, retry)\n"
        "4. Escalate to integration team if issue persists >10 minutes"
    ),
}


@dataclass
class CorrelationContext:
    """All data gathered to determine a reason code."""
    host: str = ""
    port: int = 0
    server_name: str = ""
    flow_name: str = ""
    receiving_app: str = ""
    detected_at: str = ""
    service_name: str = ""

    port_down: bool = False
    port_down_since: Optional[datetime] = None
    consecutive_port_failures: int = 0

    cpu_avg_pct: Optional[float] = None
    disk_max_pct: Optional[float] = None
    services_stopped: List[str] = field(default_factory=list)

    stuck_count: int = 0
    log_errors: int = 0
    log_error_excerpt: str = ""

    reason_code: str = "UNKNOWN_DELAY"
    confidence: str = "low"   # low | medium | high


async def correlate_no_ack(
    db: AsyncSession,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    server_name: Optional[str] = None,
    flow_id: Optional[str] = None,
    flow_name: str = "",
    receiving_app: str = "",
    detected_at: Optional[datetime] = None,
    stuck_count: int = 0,
) -> CorrelationContext:
    """
    Run all correlation checks and return a populated CorrelationContext.
    """
    window_start = (detected_at or datetime.now(timezone.utc)) - timedelta(
        seconds=settings.CORRELATION_WINDOW_SECONDS
    )
    ctx = CorrelationContext(
        host=host or "",
        port=port or 0,
        server_name=server_name or "",
        flow_name=flow_name,
        receiving_app=receiving_app,
        detected_at=(detected_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC"),
        stuck_count=stuck_count,
    )

    # --- Check 1: Port reachability ---
    if host and port:
        from app.db.models import Endpoint
        ep_result = await db.execute(
            select(Endpoint).where(Endpoint.host == host, Endpoint.port == port)
        )
        ep = ep_result.scalar_one_or_none()
        if ep:
            probe_result = await db.execute(
                select(Probe)
                .where(Probe.endpoint_id == ep.id, Probe.probed_at >= window_start)
                .order_by(Probe.probed_at.desc())
                .limit(5)
            )
            recent_probes = probe_result.scalars().all()
            if recent_probes:
                down_count = sum(1 for p in recent_probes if p.status in ("down", "timeout", "error"))
                ctx.consecutive_port_failures = down_count
                if down_count >= 2:
                    ctx.port_down = True
                    ctx.port_down_since = recent_probes[-1].probed_at if recent_probes else None

    # --- Check 2: CPU ---
    if server_name:
        cpu_result = await db.execute(
            select(func.avg(ServerMetric.cpu_percent))
            .where(
                ServerMetric.server_name == server_name,
                ServerMetric.collected_at >= window_start,
            )
        )
        ctx.cpu_avg_pct = cpu_result.scalar()

        # --- Check 3: Disk ---
        disk_result = await db.execute(
            select(func.max(ServerMetric.disk_percent))
            .where(
                ServerMetric.server_name == server_name,
                ServerMetric.collected_at >= window_start,
            )
        )
        ctx.disk_max_pct = disk_result.scalar()

        # --- Check 4: Services ---
        svc_result = await db.execute(
            select(ServerMetric.services)
            .where(ServerMetric.server_name == server_name)
            .order_by(ServerMetric.collected_at.desc())
            .limit(1)
        )
        latest_services = svc_result.scalar()
        if latest_services:
            ctx.services_stopped = [
                svc for svc, st in latest_services.items()
                if str(st).lower() in ("stopped", "failed", "dead")
            ]
            # Pick most recognizable engine service name for action message
            for svc in ctx.services_stopped:
                if any(k in svc.lower() for k in ("rhapsody", "mirth", "ensemble", "engine")):
                    ctx.service_name = svc
                    break
            if not ctx.service_name and ctx.services_stopped:
                ctx.service_name = ctx.services_stopped[0]

        # --- Check 5: Log errors (from evidence) ---
        log_err_result = await db.execute(
            select(func.count(Evidence.id))
            .join(Alert, Alert.id == Evidence.alert_id)
            .where(
                Evidence.evidence_type == "log_entry",
                Evidence.collected_at >= window_start,
                Evidence.data["level"].astext.in_(["ERROR", "FATAL", "CRITICAL"]),
                Evidence.data["server_name"].astext == server_name,
            )
        )
        ctx.log_errors = log_err_result.scalar() or 0

    # --- Check 5b: Flow congestion ---
    if flow_id:
        congestion_result = await db.execute(
            select(func.count(HL7Message.id))
            .where(
                HL7Message.flow_id == flow_id,
                HL7Message.status == "pending",
                HL7Message.detected_at >= window_start,
            )
        )
        ctx.stuck_count = congestion_result.scalar() or stuck_count

    # --- Assign reason code (priority order) ---
    ctx.reason_code, ctx.confidence = _pick_reason(ctx)
    return ctx


def _pick_reason(ctx: CorrelationContext) -> tuple[str, str]:
    """Return (reason_code, confidence)."""
    if ctx.port_down and ctx.consecutive_port_failures >= 2:
        return "NETWORK_FAILURE", "high"
    if ctx.services_stopped:
        return "ENGINE_DOWN", "high"
    if ctx.disk_max_pct is not None and ctx.disk_max_pct >= 95:
        return "DISK_FULL", "high"
    if ctx.cpu_avg_pct is not None and ctx.cpu_avg_pct >= 90:
        return "CPU_OVERLOAD", "medium"
    if ctx.stuck_count >= 20:
        return "FLOW_CONGESTION", "medium"
    if ctx.log_errors > 0:
        return "LOG_ERROR_PATTERN", "medium"
    if ctx.port_down:
        return "NETWORK_FAILURE", "medium"
    if ctx.cpu_avg_pct is not None and ctx.cpu_avg_pct >= 80:
        return "CPU_OVERLOAD", "low"
    return "UNKNOWN_DELAY", "low"


def build_action_text(ctx: CorrelationContext) -> str:
    """Render the action template with context variables."""
    template = ACTION_MAP.get(ctx.reason_code, ACTION_MAP["UNKNOWN_DELAY"])
    return template.format(
        host=ctx.host or "UNKNOWN",
        port=ctx.port or 0,
        server=ctx.server_name or "UNKNOWN",
        service=ctx.service_name or "engine-service",
        flow=ctx.flow_name or "UNKNOWN",
        receiving_app=ctx.receiving_app or "UNKNOWN",
        detected_at=ctx.detected_at,
    )


def build_bite_text(
    ctx: CorrelationContext,
    severity: str,
    message_type: str,
    stuck_messages: list,
    oldest_age_seconds: int,
) -> str:
    """
    Build the full ASCII BITE warning block.
    Returns a multi-line string, ASCII-only, safe for email/SMS/paging.
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    age_h = oldest_age_seconds // 3600
    age_m = (oldest_age_seconds % 3600) // 60
    age_s = oldest_age_seconds % 60
    if age_h > 0:
        age_str = f"{age_h}h {age_m}m {age_s}s"
    elif age_m > 0:
        age_str = f"{age_m}m {age_s}s"
    else:
        age_str = f"{age_s}s"

    sample_ids = ", ".join(m.get("message_control_id", "?") for m in stuck_messages[:3])
    extra = f" (+{len(stuck_messages)-3} more)" if len(stuck_messages) > 3 else ""

    action = build_action_text(ctx)
    action_lines = "\n".join(f"|  {line}" for line in action.split("\n"))

    sev_label = severity.upper()
    border = "=" * 64

    # Evidence lines
    def ev_line(label: str, val: Optional[float], threshold: float, unit: str, fmt: str = ".1f") -> str:
        if val is None:
            return f"|  [UNKN] {label:<35} not monitored"
        flag = "FAIL" if val >= threshold else "OK  "
        return f"|  [{flag}] {label:<35} {val:{fmt}}{unit}"

    port_line = (
        f"|  [FAIL] Port {ctx.host}:{ctx.port:<28} DOWN"
        if ctx.port_down
        else f"|  [OK  ] Port {ctx.host}:{ctx.port:<28} reachable"
    ) if ctx.host else "|  [UNKN] Port status                              not configured"

    reason_label = REASON_CODES.get(ctx.reason_code, "See action below")
    confidence_str = f"[{ctx.confidence.upper()} confidence]"

    bite = f"""
+{border}+
| BITE WARNING [{sev_label}]  {now_str:<25} |
+{'-' * 64}+
| WHAT:    {message_type} outbound -> {ctx.receiving_app or 'UNKNOWN':<26} |
|          Endpoint: {ctx.host}:{ctx.port:<44} |
|          Ctrl IDs: {(sample_ids + extra)[:48]:<48} |
+{'-' * 64}+
| IMPACT:  {ctx.stuck_count} message(s) stuck  |  Oldest age: {age_str:<16} |
+{'-' * 64}+
| EVIDENCE:                                                      |
{port_line:<66} |
{ev_line('CPU on ' + (ctx.server_name or 'server'), ctx.cpu_avg_pct, 85, '%'):<66} |
{ev_line('Disk on ' + (ctx.server_name or 'server'), ctx.disk_max_pct, 85, '%'):<66} |
|  {'[FAIL]' if ctx.services_stopped else '[OK  ]'} Services stopped: {', '.join(ctx.services_stopped) if ctx.services_stopped else 'none':<38} |
+{'-' * 64}+
| REASON:  {ctx.reason_code:<54} |
| {reason_label:<64} |
| {confidence_str:<64} |
+{'-' * 64}+
| SUGGESTED FIRST ACTION:                                        |
{action_lines}
+{border}+
""".strip()

    return bite
