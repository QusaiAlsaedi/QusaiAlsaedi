"""Agent metrics ingestion and retrieval."""
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db.session import get_db
from app.db.models import ServerMetric
from app.core.security import get_current_user
from app.api.deps import require_agent_key

router = APIRouter()


class AgentMetricsPayload(BaseModel):
    server_name: str
    platform: str = "linux"
    cpu_percent: Optional[float] = None
    memory_percent: Optional[float] = None
    memory_used_mb: Optional[int] = None
    memory_total_mb: Optional[int] = None
    disk_percent: Optional[float] = None
    disk_used_gb: Optional[float] = None
    disk_total_gb: Optional[float] = None
    load_avg_1m: Optional[float] = None
    load_avg_5m: Optional[float] = None
    load_avg_15m: Optional[float] = None
    services: Dict[str, str] = {}
    agent_version: Optional[str] = None
    collected_at: Optional[datetime] = None


class ServerSummaryOut(BaseModel):
    server_name: str
    last_seen: Optional[datetime]
    cpu_percent: Optional[float]
    memory_percent: Optional[float]
    disk_percent: Optional[float]
    services: Dict[str, str]
    platform: Optional[str]
    status: str  # healthy | warning | critical | unknown


@router.post("/ingest", status_code=status.HTTP_201_CREATED)
async def ingest_metrics(
    payload: AgentMetricsPayload,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_agent_key),
):
    metric = ServerMetric(
        server_name=payload.server_name,
        collected_at=payload.collected_at or datetime.now(timezone.utc),
        cpu_percent=payload.cpu_percent,
        memory_percent=payload.memory_percent,
        memory_used_mb=payload.memory_used_mb,
        memory_total_mb=payload.memory_total_mb,
        disk_percent=payload.disk_percent,
        disk_used_gb=payload.disk_used_gb,
        disk_total_gb=payload.disk_total_gb,
        load_avg_1m=payload.load_avg_1m,
        load_avg_5m=payload.load_avg_5m,
        load_avg_15m=payload.load_avg_15m,
        services=payload.services,
        agent_version=payload.agent_version,
        platform=payload.platform,
    )
    db.add(metric)
    await db.commit()
    return {"status": "ok"}


@router.get("/servers", response_model=List[ServerSummaryOut])
async def list_servers(
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    # Get latest metric per server
    subq = (
        select(
            ServerMetric.server_name,
            func.max(ServerMetric.collected_at).label("max_collected"),
        )
        .group_by(ServerMetric.server_name)
        .subquery()
    )
    result = await db.execute(
        select(ServerMetric)
        .join(subq, (ServerMetric.server_name == subq.c.server_name) &
                    (ServerMetric.collected_at == subq.c.max_collected))
    )
    metrics = result.scalars().all()
    out = []
    for m in metrics:
        s = _compute_status(m)
        out.append(ServerSummaryOut(
            server_name=m.server_name, last_seen=m.collected_at,
            cpu_percent=m.cpu_percent, memory_percent=m.memory_percent,
            disk_percent=m.disk_percent, services=m.services or {},
            platform=m.platform, status=s,
        ))
    return out


@router.get("/servers/{server_name}/history")
async def server_history(
    server_name: str,
    hours: int = Query(4, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = await db.execute(
        select(ServerMetric)
        .where(ServerMetric.server_name == server_name, ServerMetric.collected_at >= since)
        .order_by(ServerMetric.collected_at.asc())
    )
    return [
        {
            "collected_at": m.collected_at.isoformat(),
            "cpu_percent": m.cpu_percent,
            "memory_percent": m.memory_percent,
            "disk_percent": m.disk_percent,
            "load_avg_1m": m.load_avg_1m,
        }
        for m in result.scalars().all()
    ]


def _compute_status(m: ServerMetric) -> str:
    stale_threshold = timedelta(minutes=5)
    if (datetime.now(timezone.utc) - m.collected_at) > stale_threshold:
        return "unknown"
    stopped = [s for s, st in (m.services or {}).items() if str(st).lower() in ("stopped", "failed")]
    if stopped:
        return "critical"
    if (m.cpu_percent or 0) >= 95 or (m.disk_percent or 0) >= 95:
        return "critical"
    if (m.cpu_percent or 0) >= 85 or (m.disk_percent or 0) >= 85 or (m.memory_percent or 0) >= 90:
        return "warning"
    return "healthy"
