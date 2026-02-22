from datetime import datetime
from typing import List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db.session import get_db
from app.db.models import Endpoint, Probe
from app.core.security import get_current_user, require_role

router = APIRouter()


class EndpointCreate(BaseModel):
    name: str
    host: str
    port: int
    protocol: str = "tcp"
    check_interval_seconds: int = 30
    timeout_seconds: int = 5
    tags: list = []
    metadata: dict = {}
    enabled: bool = True


class EndpointOut(BaseModel):
    id: str
    name: str
    host: str
    port: int
    protocol: str
    check_interval_seconds: int
    timeout_seconds: int
    tags: list
    metadata: dict
    enabled: bool
    last_status: Optional[str]
    last_checked_at: Optional[datetime]
    last_latency_ms: Optional[int]
    created_at: datetime

    @classmethod
    def from_orm(cls, ep: Endpoint) -> "EndpointOut":
        return cls(
            id=str(ep.id), name=ep.name, host=ep.host, port=ep.port,
            protocol=ep.protocol, check_interval_seconds=ep.check_interval_seconds,
            timeout_seconds=ep.timeout_seconds, tags=ep.tags or [],
            metadata=ep.metadata_ or {}, enabled=ep.enabled,
            last_status=ep.last_status, last_checked_at=ep.last_checked_at,
            last_latency_ms=ep.last_latency_ms, created_at=ep.created_at,
        )


class ProbeOut(BaseModel):
    id: str
    endpoint_id: str
    probed_at: datetime
    status: str
    latency_ms: Optional[int]
    http_status: Optional[int]
    error_message: Optional[str]


@router.get("", response_model=List[EndpointOut])
async def list_endpoints(
    status_filter: Optional[str] = Query(None, alias="status"),
    enabled: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    q = select(Endpoint)
    if status_filter:
        q = q.where(Endpoint.last_status == status_filter)
    if enabled is not None:
        q = q.where(Endpoint.enabled == enabled)
    q = q.order_by(Endpoint.name)
    result = await db.execute(q)
    return [EndpointOut.from_orm(ep) for ep in result.scalars().all()]


@router.post("", response_model=EndpointOut, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    body: EndpointCreate,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin", "operator")),
):
    ep = Endpoint(
        name=body.name, host=body.host, port=body.port, protocol=body.protocol,
        check_interval_seconds=body.check_interval_seconds,
        timeout_seconds=body.timeout_seconds,
        tags=body.tags, metadata_=body.metadata, enabled=body.enabled,
    )
    db.add(ep)
    await db.commit()
    await db.refresh(ep)
    return EndpointOut.from_orm(ep)


@router.get("/{endpoint_id}", response_model=EndpointOut)
async def get_endpoint(
    endpoint_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    result = await db.execute(select(Endpoint).where(Endpoint.id == endpoint_id))
    ep = result.scalar_one_or_none()
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return EndpointOut.from_orm(ep)


@router.put("/{endpoint_id}", response_model=EndpointOut)
async def update_endpoint(
    endpoint_id: uuid.UUID,
    body: EndpointCreate,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin", "operator")),
):
    result = await db.execute(select(Endpoint).where(Endpoint.id == endpoint_id))
    ep = result.scalar_one_or_none()
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    for k, v in body.model_dump().items():
        setattr(ep, k if k != "metadata" else "metadata_", v)
    await db.commit()
    await db.refresh(ep)
    return EndpointOut.from_orm(ep)


@router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    endpoint_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin")),
):
    result = await db.execute(select(Endpoint).where(Endpoint.id == endpoint_id))
    ep = result.scalar_one_or_none()
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    await db.delete(ep)
    await db.commit()


@router.get("/{endpoint_id}/probes", response_model=List[ProbeOut])
async def endpoint_probes(
    endpoint_id: uuid.UUID,
    hours: int = Query(1, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    from datetime import timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = await db.execute(
        select(Probe)
        .where(Probe.endpoint_id == endpoint_id, Probe.probed_at >= since)
        .order_by(Probe.probed_at.desc())
        .limit(500)
    )
    probes = result.scalars().all()
    return [
        ProbeOut(
            id=str(p.id), endpoint_id=str(p.endpoint_id), probed_at=p.probed_at,
            status=p.status, latency_ms=p.latency_ms, http_status=p.http_status,
            error_message=p.error_message,
        )
        for p in probes
    ]
