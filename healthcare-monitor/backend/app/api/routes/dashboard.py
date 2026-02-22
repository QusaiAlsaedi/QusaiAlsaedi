"""Dashboard summary endpoint - aggregates all subsystem health."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import Endpoint, Probe, ServerMetric, HL7Message, Alert
from app.core.security import get_current_user

router = APIRouter()


@router.get("/summary")
async def dashboard_summary(
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    hour_ago = now - timedelta(hours=1)
    five_min_ago = now - timedelta(minutes=5)

    # --- Endpoints ---
    ep_total = await _scalar(db, select(func.count(Endpoint.id)).where(Endpoint.enabled == True))
    ep_up = await _scalar(db, select(func.count(Endpoint.id)).where(
        Endpoint.enabled == True, Endpoint.last_status == "up"))
    ep_down = await _scalar(db, select(func.count(Endpoint.id)).where(
        Endpoint.enabled == True, Endpoint.last_status == "down"))

    # --- Servers ---
    # Latest metric per server within last 5 min
    subq = (
        select(ServerMetric.server_name, func.max(ServerMetric.collected_at).label("max_at"))
        .where(ServerMetric.collected_at >= five_min_ago)
        .group_by(ServerMetric.server_name)
        .subquery()
    )
    srv_result = await db.execute(
        select(ServerMetric)
        .join(subq, (ServerMetric.server_name == subq.c.server_name) &
                    (ServerMetric.collected_at == subq.c.max_at))
    )
    servers = srv_result.scalars().all()
    srv_critical = sum(1 for s in servers if
                       (s.cpu_percent or 0) >= 95 or (s.disk_percent or 0) >= 95 or
                       any(v.lower() in ("stopped", "failed") for v in (s.services or {}).values()))
    srv_warning = sum(1 for s in servers if
                      not ((s.cpu_percent or 0) >= 95 or (s.disk_percent or 0) >= 95) and
                      ((s.cpu_percent or 0) >= 85 or (s.disk_percent or 0) >= 85 or
                       (s.memory_percent or 0) >= 90))

    # --- HL7 ---
    hl7_pending = await _scalar(db, select(func.count(HL7Message.id)).where(
        HL7Message.status == "pending"))
    hl7_stuck_30s = await _scalar(db, select(func.count(HL7Message.id)).where(
        HL7Message.status == "pending",
        HL7Message.detected_at <= now - timedelta(seconds=30)))
    hl7_acked = await _scalar(db, select(func.count(HL7Message.id)).where(
        HL7Message.status == "acked", HL7Message.detected_at >= hour_ago))
    hl7_nacked = await _scalar(db, select(func.count(HL7Message.id)).where(
        HL7Message.status == "nacked", HL7Message.detected_at >= hour_ago))
    hl7_timeout = await _scalar(db, select(func.count(HL7Message.id)).where(
        HL7Message.status == "timeout", HL7Message.detected_at >= hour_ago))

    # --- Alerts ---
    alerts_result = await db.execute(
        select(Alert.severity, func.count(Alert.id))
        .where(Alert.status == "open")
        .group_by(Alert.severity)
    )
    alert_counts = {row[0]: row[1] for row in alerts_result.all()}

    return {
        "endpoints": {
            "total": ep_total,
            "up": ep_up,
            "down": ep_down,
            "unknown": ep_total - ep_up - ep_down,
        },
        "servers": {
            "total": len(servers),
            "healthy": len(servers) - srv_critical - srv_warning,
            "warning": srv_warning,
            "critical": srv_critical,
        },
        "hl7": {
            "pending": hl7_pending,
            "stuck_30s": hl7_stuck_30s,
            "acked_1h": hl7_acked,
            "nacked_1h": hl7_nacked,
            "timeout_1h": hl7_timeout,
        },
        "alerts": {
            "open_critical": alert_counts.get("critical", 0),
            "open_warning": alert_counts.get("warning", 0),
            "open_info": alert_counts.get("info", 0),
        },
        "last_updated": now.isoformat(),
    }


async def _scalar(db, query):
    result = await db.execute(query)
    return result.scalar() or 0
