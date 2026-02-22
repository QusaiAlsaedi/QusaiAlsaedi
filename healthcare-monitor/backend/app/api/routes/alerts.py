from datetime import datetime, timezone
from typing import List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db.session import get_db
from app.db.models import Alert, Evidence, User
from app.core.security import get_current_user, require_role

router = APIRouter()


class EvidenceOut(BaseModel):
    id: str
    evidence_type: str
    collected_at: datetime
    data: dict
    description: Optional[str]


class AlertOut(BaseModel):
    id: str
    alert_type: str
    severity: str
    title: str
    description: Optional[str]
    reason_code: Optional[str]
    status: str
    created_at: datetime
    acknowledged_at: Optional[datetime]
    resolved_at: Optional[datetime]
    target_type: Optional[str]
    target_id: Optional[str]
    metadata: dict
    evidence: List[EvidenceOut] = []
    bite_text: Optional[str] = None

    @classmethod
    def from_orm(cls, a: Alert, include_bite: bool = False, include_evidence: bool = False) -> "AlertOut":
        ev = []
        if include_evidence:
            ev = [EvidenceOut(id=str(e.id), evidence_type=e.evidence_type,
                              collected_at=e.collected_at, data=e.data,
                              description=e.description) for e in a.evidence]
        return cls(
            id=str(a.id), alert_type=a.alert_type, severity=a.severity,
            title=a.title, description=a.description, reason_code=a.reason_code,
            status=a.status, created_at=a.created_at,
            acknowledged_at=a.acknowledged_at, resolved_at=a.resolved_at,
            target_type=a.target_type, target_id=a.target_id,
            metadata=a.metadata_ or {}, evidence=ev,
            bite_text=a.bite_text if include_bite else None,
        )


@router.get("", response_model=List[AlertOut])
async def list_alerts(
    alert_status: Optional[str] = Query(None, alias="status"),
    severity: Optional[str] = None,
    alert_type: Optional[str] = Query(None, alias="type"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    q = select(Alert).order_by(Alert.created_at.desc())
    if alert_status:
        q = q.where(Alert.status == alert_status)
    if severity:
        q = q.where(Alert.severity == severity)
    if alert_type:
        q = q.where(Alert.alert_type == alert_type)
    result = await db.execute(q.limit(limit))
    return [AlertOut.from_orm(a) for a in result.scalars().all()]


@router.get("/active/count")
async def active_count(
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    from sqlalchemy import func
    result = await db.execute(
        select(Alert.severity, func.count(Alert.id))
        .where(Alert.status == "open")
        .group_by(Alert.severity)
    )
    counts = {row[0]: row[1] for row in result.all()}
    return {
        "critical": counts.get("critical", 0),
        "warning": counts.get("warning", 0),
        "info": counts.get("info", 0),
    }


@router.get("/{alert_id}", response_model=AlertOut)
async def get_alert(
    alert_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    from sqlalchemy.orm import selectinload
    result = await db.execute(
        select(Alert).options(selectinload(Alert.evidence)).where(Alert.id == alert_id)
    )
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Alert not found")
    return AlertOut.from_orm(a, include_bite=True, include_evidence=True)


@router.get("/bite/{alert_id}")
async def get_bite(
    alert_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"bite_text": a.bite_text or "(No BITE text generated for this alert)"}


@router.post("/{alert_id}/acknowledge", status_code=status.HTTP_200_OK)
async def acknowledge_alert(
    alert_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role("admin", "operator")),
):
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Alert not found")
    if a.status != "open":
        raise HTTPException(status_code=400, detail=f"Alert is already {a.status}")
    a.status = "acked"
    a.acknowledged_at = datetime.now(timezone.utc)
    a.acknowledged_by = current_user.id
    await db.commit()
    return {"status": "acknowledged"}


@router.post("/{alert_id}/resolve", status_code=status.HTTP_200_OK)
async def resolve_alert(
    alert_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role("admin", "operator")),
):
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Alert not found")
    a.status = "resolved"
    a.resolved_at = datetime.now(timezone.utc)
    a.resolved_by = current_user.id
    await db.commit()
    return {"status": "resolved"}
