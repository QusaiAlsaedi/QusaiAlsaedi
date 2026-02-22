"""
HL7 No-ACK tracker.

Every ACK_SWEEP_INTERVAL_SECONDS this task:
  1. Finds pending messages older than their flow's ack_timeout_seconds
  2. Marks them as 'timeout'
  3. Triggers alert creation via the alert engine

The alert engine does the reason-code correlation.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List

import structlog
from sqlalchemy import select, update

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.db.models import HL7Message, HL7Flow, Alert, Evidence

log = structlog.get_logger(__name__)


async def _sweep_timeouts() -> List[HL7Message]:
    """
    Find all pending outbound messages older than their flow's ack_timeout_seconds
    (or the global default) and mark them as 'timeout'.
    Returns the list of messages that just timed out.
    """
    now = datetime.now(timezone.utc)
    timed_out: List[HL7Message] = []

    async with AsyncSessionLocal() as db:
        # Load flows to get per-flow timeouts
        flow_result = await db.execute(
            select(HL7Flow).where(HL7Flow.enabled == True)
        )
        flows = {str(f.id): f for f in flow_result.scalars().all()}

        # Get all pending outbound messages
        result = await db.execute(
            select(HL7Message).where(
                HL7Message.status == "pending",
                HL7Message.direction == "outbound",
            )
        )
        pending = result.scalars().all()

        for msg in pending:
            # Determine timeout threshold
            if msg.flow_id and str(msg.flow_id) in flows:
                timeout_s = flows[str(msg.flow_id)].ack_timeout_seconds
            else:
                timeout_s = settings.DEFAULT_ACK_TIMEOUT_SECONDS

            age_s = (now - msg.detected_at).total_seconds()
            if age_s >= timeout_s:
                msg.status = "timeout"
                timed_out.append(msg)
                log.warning(
                    "HL7 message timed out",
                    ctrl_id=msg.message_control_id,
                    msg_type=msg.message_type,
                    age_seconds=int(age_s),
                    server=msg.server_name,
                )

        if timed_out:
            await db.commit()

    return timed_out


async def run_hl7_tracker_loop() -> None:
    """
    Background loop: sweeps for timeouts, notifies alert engine queue.
    """
    log.info("HL7 tracker started")
    await asyncio.sleep(5)  # stagger startup

    while True:
        try:
            timed_out = await _sweep_timeouts()
            if timed_out:
                # Emit a minimal signal for alert engine to pick up.
                # The alert engine queries the DB directly on its own cycle,
                # so we just need to ensure the status is persisted (done above).
                log.info("hl7 timeout sweep", count=len(timed_out))
        except asyncio.CancelledError:
            log.info("HL7 tracker cancelled")
            return
        except Exception as e:
            log.error("HL7 tracker error", error=str(e))

        await asyncio.sleep(settings.ACK_SWEEP_INTERVAL_SECONDS)
