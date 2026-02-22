from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, BackgroundTasks
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db.session import get_db
from app.db.models import Probe, Endpoint
from app.core.security import get_current_user, require_role

router = APIRouter()


class ProbeOut(BaseModel):
    id: str
    endpoint_id: str
    endpoint_name: str
    host: str
    port: int
    probed_at: datetime
    status: str
    latency_ms: Optional[int]
    error_message: Optional[str]


@router.get("/latest", response_model=List[ProbeOut])
async def latest_probes(
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    # Latest probe per endpoint
    subq = (
        select(Probe.endpoint_id, func.max(Probe.probed_at).label("max_at"))
        .group_by(Probe.endpoint_id)
        .subquery()
    )
    result = await db.execute(
        select(Probe, Endpoint.name, Endpoint.host, Endpoint.port)
        .join(subq, (Probe.endpoint_id == subq.c.endpoint_id) &
                    (Probe.probed_at == subq.c.max_at))
        .join(Endpoint, Probe.endpoint_id == Endpoint.id)
        .order_by(Probe.probed_at.desc())
        .limit(limit)
    )
    rows = result.all()
    return [
        ProbeOut(
            id=str(row[0].id), endpoint_id=str(row[0].endpoint_id),
            endpoint_name=row[1], host=row[2], port=row[3],
            probed_at=row[0].probed_at, status=row[0].status,
            latency_ms=row[0].latency_ms, error_message=row[0].error_message,
        )
        for row in rows
    ]


@router.get("/summary")
async def probe_summary(
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    """Count of endpoints by current status."""
    subq = (
        select(Probe.endpoint_id, func.max(Probe.probed_at).label("max_at"))
        .group_by(Probe.endpoint_id)
        .subquery()
    )
    result = await db.execute(
        select(Probe.status, func.count(Probe.id))
        .join(subq, (Probe.endpoint_id == subq.c.endpoint_id) &
                    (Probe.probed_at == subq.c.max_at))
        .group_by(Probe.status)
    )
    counts = {row[0]: row[1] for row in result.all()}
    total = sum(counts.values())
    return {
        "total": total,
        "up": counts.get("up", 0),
        "down": counts.get("down", 0),
        "timeout": counts.get("timeout", 0),
        "error": counts.get("error", 0),
    }


@router.post("/trigger")
async def trigger_probes(
    body: dict,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin", "operator")),
):
    """Manually trigger probes for specified endpoint IDs (or all if empty)."""
    from app.services.poller import probe_endpoint_by_id
    endpoint_ids = body.get("endpoint_ids", [])
    if endpoint_ids:
        q = select(Endpoint).where(Endpoint.id.in_(endpoint_ids), Endpoint.enabled == True)
    else:
        q = select(Endpoint).where(Endpoint.enabled == True)
    result = await db.execute(q)
    endpoints = result.scalars().all()
    for ep in endpoints:
        background_tasks.add_task(probe_endpoint_by_id, ep.id)
    return {"triggered": len(endpoints)}
