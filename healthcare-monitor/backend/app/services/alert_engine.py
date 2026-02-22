"""
Alert Engine - evaluates alert rules and fires alerts with BITE blocks.

Runs every ALERT_ENGINE_INTERVAL_SECONDS and checks:
  1. Port down (from recent probes)
  2. No-ACK timeout messages needing an open alert
  3. High CPU / high disk / service down (from server metrics)
  4. NACK rate
"""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, List
import uuid

import structlog
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.db.models import (
    Alert, AlertRule, Endpoint, Probe, ServerMetric, HL7Message, HL7Flow, Evidence
)
from app.core.reason_codes import correlate_no_ack, build_bite_text

log = structlog.get_logger(__name__)


async def _open_alert_exists(db: AsyncSession, alert_type: str, target_id: str,
                              window_minutes: int = 30) -> Optional[Alert]:
    """Return existing open alert of same type/target within window, or None."""
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    result = await db.execute(
        select(Alert).where(
            Alert.alert_type == alert_type,
            Alert.target_id == target_id,
            Alert.status == "open",
            Alert.created_at >= since,
        ).limit(1)
    )
    return result.scalar_one_or_none()


async def _create_alert(
    db: AsyncSession,
    alert_type: str,
    severity: str,
    title: str,
    description: str,
    target_type: str,
    target_id: str,
    reason_code: Optional[str] = None,
    bite_text: Optional[str] = None,
    metadata: Optional[dict] = None,
    evidence_items: Optional[list] = None,
) -> Alert:
    alert = Alert(
        alert_type=alert_type, severity=severity, title=title,
        description=description, target_type=target_type, target_id=target_id,
        reason_code=reason_code, bite_text=bite_text, metadata_=metadata or {},
    )
    db.add(alert)
    await db.flush()  # get ID before adding evidence

    for ev in (evidence_items or []):
        db.add(Evidence(
            alert_id=alert.id,
            evidence_type=ev["type"],
            data=ev["data"],
            description=ev.get("description"),
        ))

    await db.commit()
    log.warning("alert fired", type=alert_type, severity=severity, title=title)

    # Fire webhook if configured (non-blocking)
    if settings.WEBHOOK_URL:
        asyncio.create_task(_fire_webhook(alert, bite_text))

    return alert


async def _fire_webhook(alert: Alert, bite_text: Optional[str]) -> None:
    import aiohttp
    payload = {
        "id": str(alert.id),
        "alert_type": alert.alert_type,
        "severity": alert.severity,
        "title": alert.title,
        "reason_code": alert.reason_code,
        "target_id": alert.target_id,
        "created_at": alert.created_at.isoformat(),
        "bite_text": bite_text or "",
    }
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(settings.WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=5))
    except Exception as e:
        log.error("webhook failed", error=str(e))


# ---------------------------------------------------------------------------
# Rule evaluators
# ---------------------------------------------------------------------------

async def check_port_down(db: AsyncSession) -> None:
    """Fire PORT_DOWN alert when an endpoint has 2+ consecutive failures."""
    result = await db.execute(select(Endpoint).where(Endpoint.enabled == True))
    endpoints = result.scalars().all()

    for ep in endpoints:
        recent = await db.execute(
            select(Probe)
            .where(Probe.endpoint_id == ep.id)
            .order_by(Probe.probed_at.desc())
            .limit(3)
        )
        probes = recent.scalars().all()
        if len(probes) < 2:
            continue
        down_count = sum(1 for p in probes[:3] if p.status in ("down", "timeout", "error"))
        if down_count < 2:
            continue

        target_id = f"endpoint:{ep.id}"
        existing = await _open_alert_exists(db, "PORT_DOWN", target_id, window_minutes=30)
        if existing:
            continue

        latest = probes[0]
        await _create_alert(
            db, alert_type="PORT_DOWN", severity="critical",
            title=f"Port down: {ep.name} ({ep.host}:{ep.port})",
            description=(
                f"Endpoint '{ep.name}' ({ep.protocol.upper()} {ep.host}:{ep.port}) "
                f"has failed {down_count} consecutive probe(s). "
                f"Last status: {latest.status}. "
                f"Error: {latest.error_message or 'none'}"
            ),
            target_type="endpoint", target_id=target_id,
            reason_code="NETWORK_FAILURE",
            evidence_items=[{
                "type": "port_probe",
                "data": {
                    "endpoint_id": str(ep.id), "host": ep.host, "port": ep.port,
                    "status": latest.status, "error": latest.error_message,
                    "consecutive_failures": down_count,
                },
                "description": f"{ep.host}:{ep.port} is {latest.status}",
            }],
        )


async def check_no_ack_timeouts(db: AsyncSession) -> None:
    """
    For each group of timeout messages by flow/receiving_app, fire or update NO_ACK alert.
    """
    # Group stuck/timeout messages by (receiving_application, message_type)
    since_2h = datetime.now(timezone.utc) - timedelta(hours=2)
    result = await db.execute(
        select(HL7Message).where(
            HL7Message.status == "timeout",
            HL7Message.detected_at >= since_2h,
        ).order_by(HL7Message.detected_at.asc())
    )
    timeout_msgs: List[HL7Message] = result.scalars().all()
    if not timeout_msgs:
        return

    # Group by (receiving_application, message_type)
    groups: dict[str, list] = {}
    for msg in timeout_msgs:
        key = f"{msg.receiving_application}|{msg.message_type}"
        groups.setdefault(key, []).append(msg)

    for group_key, msgs in groups.items():
        recv_app, msg_type = group_key.split("|", 1)
        target_id = f"hl7:{group_key}"
        existing = await _open_alert_exists(db, "NO_ACK_TIMEOUT", target_id, window_minutes=60)
        if existing:
            continue

        oldest = msgs[0]
        now = datetime.now(timezone.utc)
        oldest_age_s = int((now - oldest.detected_at).total_seconds())

        # Escalate severity based on age and count
        if oldest_age_s > 300 or len(msgs) > 10:
            severity = "critical"
        elif oldest_age_s > 60 or len(msgs) > 3:
            severity = "warning"
        else:
            severity = "info"

        # Load flow info for correlation
        flow = None
        if oldest.flow_id:
            flow_result = await db.execute(
                select(HL7Flow).where(HL7Flow.id == oldest.flow_id)
            )
            flow = flow_result.scalar_one_or_none()

        # Find target endpoint for correlation
        target_ep = await _find_endpoint_for_app(db, recv_app)

        ctx = await correlate_no_ack(
            db,
            host=target_ep.host if target_ep else None,
            port=target_ep.port if target_ep else None,
            server_name=oldest.server_name,
            flow_id=str(oldest.flow_id) if oldest.flow_id else None,
            flow_name=flow.name if flow else group_key,
            receiving_app=recv_app,
            detected_at=oldest.detected_at,
            stuck_count=len(msgs),
        )

        bite = build_bite_text(
            ctx=ctx, severity=severity, message_type=msg_type,
            stuck_messages=[{"message_control_id": m.message_control_id} for m in msgs],
            oldest_age_seconds=oldest_age_s,
        )

        ev_items = [
            {
                "type": "no_ack",
                "data": {
                    "stuck_count": len(msgs),
                    "oldest_ctrl_id": oldest.message_control_id,
                    "oldest_age_seconds": oldest_age_s,
                    "message_type": msg_type,
                    "receiving_app": recv_app,
                },
                "description": f"{len(msgs)} {msg_type} message(s) with no ACK",
            }
        ]
        if ctx.port_down:
            ev_items.append({
                "type": "port_probe",
                "data": {"host": ctx.host, "port": ctx.port, "status": "down",
                         "consecutive_failures": ctx.consecutive_port_failures},
                "description": f"Port {ctx.host}:{ctx.port} is down",
            })
        if ctx.cpu_avg_pct is not None:
            ev_items.append({
                "type": "cpu_spike",
                "data": {"server_name": ctx.server_name, "cpu_avg_pct": ctx.cpu_avg_pct},
                "description": f"CPU avg {ctx.cpu_avg_pct:.1f}% on {ctx.server_name}",
            })

        await _create_alert(
            db, alert_type="NO_ACK_TIMEOUT", severity=severity,
            title=f"No ACK: {len(msgs)} {msg_type} message(s) stuck -> {recv_app}",
            description=(
                f"{len(msgs)} outbound {msg_type} message(s) to '{recv_app}' "
                f"have no ACK. Oldest: {oldest_age_s}s ago. "
                f"Reason: {ctx.reason_code}"
            ),
            target_type="hl7_flow", target_id=target_id,
            reason_code=ctx.reason_code, bite_text=bite,
            evidence_items=ev_items,
        )


async def check_server_health(db: AsyncSession) -> None:
    """Fire alerts for high CPU, high disk, or stopped services."""
    since = datetime.now(timezone.utc) - timedelta(minutes=5)

    # Latest metric per server in last 5 min
    subq = (
        select(ServerMetric.server_name, func.max(ServerMetric.collected_at).label("max_at"))
        .where(ServerMetric.collected_at >= since)
        .group_by(ServerMetric.server_name)
        .subquery()
    )
    result = await db.execute(
        select(ServerMetric)
        .join(subq, (ServerMetric.server_name == subq.c.server_name) &
                    (ServerMetric.collected_at == subq.c.max_at))
    )
    metrics = result.scalars().all()

    for m in metrics:
        # High CPU
        if m.cpu_percent is not None:
            sev = "critical" if m.cpu_percent >= 95 else ("warning" if m.cpu_percent >= 85 else None)
            if sev:
                tid = f"server_cpu:{m.server_name}"
                if not await _open_alert_exists(db, "HIGH_CPU", tid, 15):
                    await _create_alert(
                        db, "HIGH_CPU", sev,
                        title=f"High CPU on {m.server_name}: {m.cpu_percent:.1f}%",
                        description=f"CPU at {m.cpu_percent:.1f}% on {m.server_name}",
                        target_type="server", target_id=tid,
                        reason_code="CPU_OVERLOAD",
                        evidence_items=[{"type": "cpu_spike",
                                         "data": {"server_name": m.server_name, "cpu_percent": m.cpu_percent},
                                         "description": f"CPU {m.cpu_percent:.1f}%"}],
                    )

        # High disk
        if m.disk_percent is not None:
            sev = "critical" if m.disk_percent >= 95 else ("warning" if m.disk_percent >= 85 else None)
            if sev:
                tid = f"server_disk:{m.server_name}"
                if not await _open_alert_exists(db, "HIGH_DISK", tid, 30):
                    await _create_alert(
                        db, "HIGH_DISK", sev,
                        title=f"High disk on {m.server_name}: {m.disk_percent:.1f}%",
                        description=f"Disk at {m.disk_percent:.1f}% on {m.server_name}",
                        target_type="server", target_id=tid,
                        reason_code="DISK_FULL",
                        evidence_items=[{"type": "disk_usage",
                                         "data": {"server_name": m.server_name, "disk_percent": m.disk_percent},
                                         "description": f"Disk {m.disk_percent:.1f}%"}],
                    )

        # Services down
        stopped_svcs = [s for s, st in (m.services or {}).items()
                        if str(st).lower() in ("stopped", "failed", "dead")]
        for svc in stopped_svcs:
            tid = f"service:{m.server_name}:{svc}"
            if not await _open_alert_exists(db, "SERVICE_DOWN", tid, 30):
                await _create_alert(
                    db, "SERVICE_DOWN", "critical",
                    title=f"Service stopped: {svc} on {m.server_name}",
                    description=f"Service '{svc}' is stopped/failed on {m.server_name}",
                    target_type="server", target_id=tid,
                    reason_code="ENGINE_DOWN",
                    evidence_items=[{"type": "service_status",
                                     "data": {"server_name": m.server_name, "service": svc, "status": "stopped"},
                                     "description": f"{svc} is stopped"}],
                )


async def _find_endpoint_for_app(db: AsyncSession, app_name: str) -> Optional[Endpoint]:
    """Try to find an endpoint tagged with or named for this application."""
    result = await db.execute(
        select(Endpoint).where(
            Endpoint.enabled == True,
            Endpoint.tags.contains([app_name.lower()])
        ).limit(1)
    )
    ep = result.scalar_one_or_none()
    if ep:
        return ep
    # Fallback: name contains app_name
    result2 = await db.execute(
        select(Endpoint).where(
            Endpoint.enabled == True,
            Endpoint.name.ilike(f"%{app_name}%")
        ).limit(1)
    )
    return result2.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
async def run_alert_engine_loop() -> None:
    log.info("alert engine started")
    await asyncio.sleep(10)  # let poller run first

    while True:
        try:
            async with AsyncSessionLocal() as db:
                await check_port_down(db)
                await check_no_ack_timeouts(db)
                await check_server_health(db)
        except asyncio.CancelledError:
            log.info("alert engine cancelled")
            return
        except Exception as e:
            log.error("alert engine error", error=str(e))

        await asyncio.sleep(settings.ALERT_ENGINE_INTERVAL_SECONDS)
