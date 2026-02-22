"""HL7 message ingestion and no-ACK tracking routes."""
from datetime import datetime, timedelta, timezone
from typing import List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db.session import get_db
from app.db.models import HL7Message, HL7Ack, HL7Flow, LogParseRule
from app.core.security import get_current_user, require_role
from app.api.deps import require_agent_key
from app.core.phi_masking import mask_hl7_segment, safe_msh_for_storage

router = APIRouter()


# ---------------------------------------------------------------------------
# Ingest schemas
# ---------------------------------------------------------------------------
class HL7MessageItem(BaseModel):
    message_control_id: str
    message_type: str
    event_type: Optional[str] = None
    sending_application: Optional[str] = None
    sending_facility: Optional[str] = None
    receiving_application: Optional[str] = None
    receiving_facility: Optional[str] = None
    message_datetime: Optional[datetime] = None
    direction: str = "outbound"
    raw_header_masked: Optional[str] = None  # Agent must pre-mask before sending
    patient_id_masked: Optional[str] = None
    log_source_file: Optional[str] = None
    log_line_number: Optional[int] = None
    flow_id: Optional[str] = None


class HL7IngestPayload(BaseModel):
    server_name: str
    source: str  # mllp|file|log|agent
    messages: List[HL7MessageItem]


class HL7AckItem(BaseModel):
    message_control_id: str
    ack_code: str  # AA|AE|AR
    ack_datetime: Optional[datetime] = None
    error_condition: Optional[str] = None
    raw_header_masked: Optional[str] = None


class HL7AckPayload(BaseModel):
    server_name: str
    source: str
    acks: List[HL7AckItem]


# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------
class HL7MessageOut(BaseModel):
    id: str
    message_control_id: str
    message_type: str
    event_type: Optional[str]
    sending_application: Optional[str]
    receiving_application: Optional[str]
    detected_at: datetime
    status: str
    ack_received_at: Optional[datetime]
    ack_latency_ms: Optional[int]
    server_name: Optional[str]
    flow_name: Optional[str] = None

    @classmethod
    def from_orm(cls, m: HL7Message, flow_name: str | None = None) -> "HL7MessageOut":
        return cls(
            id=str(m.id), message_control_id=m.message_control_id,
            message_type=m.message_type, event_type=m.event_type,
            sending_application=m.sending_application,
            receiving_application=m.receiving_application,
            detected_at=m.detected_at, status=m.status,
            ack_received_at=m.ack_received_at, ack_latency_ms=m.ack_latency_ms,
            server_name=m.server_name, flow_name=flow_name,
        )


class StuckMessageOut(BaseModel):
    id: str
    message_control_id: str
    message_type: str
    sending_application: Optional[str]
    receiving_application: Optional[str]
    detected_at: datetime
    age_seconds: int
    age_human: str
    server_name: Optional[str]
    flow_name: Optional[str]


# ---------------------------------------------------------------------------
# Ingestion endpoints (agent pushes here)
# ---------------------------------------------------------------------------
@router.post("/ingest", status_code=status.HTTP_201_CREATED)
async def ingest_messages(
    payload: HL7IngestPayload,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_agent_key),
):
    new_count = 0
    for item in payload.messages:
        # Skip if already tracked (idempotent)
        existing = await db.execute(
            select(HL7Message).where(
                HL7Message.message_control_id == item.message_control_id,
                HL7Message.sending_application == item.sending_application,
                HL7Message.sending_facility == item.sending_facility,
            )
        )
        if existing.scalar_one_or_none():
            continue

        msg = HL7Message(
            message_control_id=item.message_control_id,
            message_type=item.message_type,
            event_type=item.event_type,
            sending_application=item.sending_application,
            sending_facility=item.sending_facility,
            receiving_application=item.receiving_application,
            receiving_facility=item.receiving_facility,
            message_datetime=item.message_datetime,
            detected_at=item.message_datetime or datetime.now(timezone.utc),
            source=payload.source,
            direction=item.direction,
            raw_header_masked=item.raw_header_masked,
            patient_id_masked=item.patient_id_masked,
            log_source_file=item.log_source_file,
            log_line_number=item.log_line_number,
            server_name=payload.server_name,
            flow_id=uuid.UUID(item.flow_id) if item.flow_id else None,
        )
        db.add(msg)
        new_count += 1

    await db.commit()
    return {"status": "ok", "ingested": new_count}


@router.post("/ack/ingest", status_code=status.HTTP_201_CREATED)
async def ingest_acks(
    payload: HL7AckPayload,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_agent_key),
):
    matched = 0
    for item in payload.acks:
        ack = HL7Ack(
            message_control_id=item.message_control_id,
            ack_code=item.ack_code,
            ack_datetime=item.ack_datetime or datetime.now(timezone.utc),
            source=payload.source,
            error_condition=item.error_condition,
            raw_header_masked=item.raw_header_masked,
            server_name=payload.server_name,
        )
        db.add(ack)

        # Update the original message
        result = await db.execute(
            select(HL7Message).where(
                HL7Message.message_control_id == item.message_control_id,
                HL7Message.status == "pending",
            )
        )
        msg = result.scalar_one_or_none()
        if msg:
            msg.status = "acked" if item.ack_code == "AA" else "nacked"
            ack_ts = item.ack_datetime or datetime.now(timezone.utc)
            msg.ack_received_at = ack_ts
            msg.ack_latency_ms = int((ack_ts - msg.detected_at).total_seconds() * 1000)
            matched += 1

    await db.commit()
    return {"status": "ok", "matched": matched}


# ---------------------------------------------------------------------------
# Query endpoints
# ---------------------------------------------------------------------------
@router.get("/messages", response_model=List[HL7MessageOut])
async def list_messages(
    msg_status: Optional[str] = Query(None, alias="status"),
    msg_type: Optional[str] = Query(None, alias="type"),
    hours: int = Query(1, ge=1, le=168),
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    q = select(HL7Message).where(HL7Message.detected_at >= since)
    if msg_status:
        q = q.where(HL7Message.status == msg_status)
    if msg_type:
        q = q.where(HL7Message.message_type == msg_type)
    q = q.order_by(HL7Message.detected_at.desc()).limit(limit)
    result = await db.execute(q)
    return [HL7MessageOut.from_orm(m) for m in result.scalars().all()]


@router.get("/stuck", response_model=List[StuckMessageOut])
async def stuck_messages(
    min_age_seconds: int = Query(30, ge=5),
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=min_age_seconds)
    result = await db.execute(
        select(HL7Message, HL7Flow.name)
        .outerjoin(HL7Flow, HL7Message.flow_id == HL7Flow.id)
        .where(HL7Message.status == "pending", HL7Message.detected_at <= cutoff)
        .order_by(HL7Message.detected_at.asc())
        .limit(500)
    )
    rows = result.all()
    now = datetime.now(timezone.utc)
    out = []
    for msg, flow_name in rows:
        age_s = int((now - msg.detected_at).total_seconds())
        out.append(StuckMessageOut(
            id=str(msg.id), message_control_id=msg.message_control_id,
            message_type=msg.message_type,
            sending_application=msg.sending_application,
            receiving_application=msg.receiving_application,
            detected_at=msg.detected_at, age_seconds=age_s,
            age_human=_fmt_age(age_s), server_name=msg.server_name,
            flow_name=flow_name,
        ))
    return out


@router.get("/stats")
async def hl7_stats(
    hours: int = Query(1, ge=1, le=24),
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = await db.execute(
        select(HL7Message.status, func.count(HL7Message.id))
        .where(HL7Message.detected_at >= since)
        .group_by(HL7Message.status)
    )
    counts = {row[0]: row[1] for row in result.all()}
    return {
        "window_hours": hours,
        "pending": counts.get("pending", 0),
        "acked": counts.get("acked", 0),
        "nacked": counts.get("nacked", 0),
        "timeout": counts.get("timeout", 0),
        "error": counts.get("error", 0),
        "total": sum(counts.values()),
    }


@router.get("/flows")
async def list_flows(
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    result = await db.execute(select(HL7Flow).order_by(HL7Flow.name))
    flows = result.scalars().all()
    return [
        {"id": str(f.id), "name": f.name, "message_types": f.message_types,
         "ack_timeout_seconds": f.ack_timeout_seconds, "transport": f.transport,
         "enabled": f.enabled}
        for f in flows
    ]


def _fmt_age(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"
