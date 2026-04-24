"""
WorkflowTracker
===============
Determines each case's current stage, computes time-in-stage,
calculates per-transition durations, and flags stuck cases.

Logic
-----
A case is at stage S when the timestamp that opens S is set but the
timestamp that opens S+1 is not yet set.  The stage clock starts at
the opening timestamp of S; time-in-stage = now − that timestamp.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from radiology_command_center.config import (
    STAGE_START_COLUMN,
    STAGE_TRANSITIONS,
    STUCK_THRESHOLDS,
    WORKFLOW_STAGES,
)
from radiology_command_center.models import RadiologyCase

logger = logging.getLogger(__name__)

# Ordered list of (stage, opening_timestamp_attr) pairs — skips COMPLETE
_STAGE_SEQUENCE: List[Tuple[str, str]] = [
    (stage, STAGE_START_COLUMN[stage])
    for stage in WORKFLOW_STAGES
    if stage != "COMPLETE"
]


def _get_ts(case: RadiologyCase, attr: str) -> Optional[datetime]:
    return getattr(case, attr, None)


def resolve_current_stage(case: RadiologyCase) -> str:
    """
    Walk the stage sequence and return the last stage whose opening
    timestamp is set AND whose successor's opening timestamp is not.
    """
    last_reached = "ORDER"

    for idx, (stage, ts_attr) in enumerate(_STAGE_SEQUENCE):
        ts = _get_ts(case, ts_attr)
        if ts is None:
            break
        last_reached = stage

        # Special case: REPORT stage is done when report_complete_time is set
        if stage == "REPORT" and case.report_complete_time is not None:
            return "COMPLETE"

    return last_reached


def compute_stage_durations(
    case: RadiologyCase,
    now: Optional[datetime] = None,
) -> Dict[str, float]:
    """
    Return a dict of {transition_label: minutes} for each completed
    (or in-progress for the current stage) transition.
    """
    if now is None:
        now = datetime.now()

    durations: Dict[str, float] = {}
    for from_stage, to_stage, label in STAGE_TRANSITIONS:
        from_attr = STAGE_START_COLUMN[from_stage]
        to_attr = STAGE_START_COLUMN[to_stage]
        t_start = _get_ts(case, from_attr)
        t_end = _get_ts(case, to_attr)

        if t_start is None:
            continue
        if t_end is not None:
            durations[label] = max(0.0, (t_end - t_start).total_seconds() / 60)
        elif case.current_stage == from_stage:
            # Stage is still open — measure time so far
            durations[label] = max(0.0, (now - t_start).total_seconds() / 60)

    return durations


def compute_time_in_stage(
    case: RadiologyCase,
    now: Optional[datetime] = None,
) -> float:
    """Minutes the case has been in its current stage."""
    if now is None:
        now = datetime.now()

    if case.current_stage is None or case.current_stage == "COMPLETE":
        return 0.0

    opening_attr = STAGE_START_COLUMN.get(case.current_stage)
    if not opening_attr:
        return 0.0

    ts = _get_ts(case, opening_attr)
    if ts is None:
        return 0.0

    return max(0.0, (now - ts).total_seconds() / 60)


def detect_stuck(
    case: RadiologyCase,
) -> Tuple[bool, Optional[str]]:
    """
    Returns (is_stuck, reason_string).
    A case is stuck when its time_in_stage exceeds the priority-specific
    threshold for the current stage.
    """
    if case.current_stage in (None, "COMPLETE"):
        return False, None

    thresholds = STUCK_THRESHOLDS.get(case.priority_norm, STUCK_THRESHOLDS["ROUTINE"])
    threshold = thresholds.get(case.current_stage)
    if threshold is None:
        return False, None

    if case.time_in_stage_minutes > threshold:
        reason = (
            f"{case.priority_norm} {case.modality_norm} at {case.current_stage} "
            f"for {case.time_in_stage_minutes:.0f} min "
            f"(threshold {threshold} min)"
        )
        return True, reason

    return False, None


def compute_total_elapsed(
    case: RadiologyCase,
    now: Optional[datetime] = None,
) -> float:
    """Total minutes from order_time to now (or to report_complete if done)."""
    if case.order_time is None:
        return 0.0

    if now is None:
        now = datetime.now()

    end = case.report_complete_time if case.report_complete_time else now
    return max(0.0, (end - case.order_time).total_seconds() / 60)


# ── Public API ───────────────────────────────────────────────────────────────

class WorkflowTracker:
    """
    Enriches a list of RadiologyCase objects with derived workflow fields.

    Call `process(cases)` to populate:
      - current_stage
      - time_in_stage_minutes
      - total_elapsed_minutes
      - stage_durations
      - is_stuck / stuck_reason
    """

    def __init__(self, reference_time: Optional[datetime] = None):
        self.now = reference_time or datetime.now()

    def process(self, cases: List[RadiologyCase]) -> List[RadiologyCase]:
        for case in cases:
            self._enrich(case)
        return cases

    def _enrich(self, case: RadiologyCase) -> None:
        # 1. Resolve stage
        case.current_stage = resolve_current_stage(case)

        # 2. Time in current stage
        case.time_in_stage_minutes = compute_time_in_stage(case, self.now)

        # 3. Total elapsed since order
        case.total_elapsed_minutes = compute_total_elapsed(case, self.now)

        # 4. Per-transition durations
        case.stage_durations = compute_stage_durations(case, self.now)

        # 5. Stuck detection
        case.is_stuck, case.stuck_reason = detect_stuck(case)

        if case.is_stuck:
            logger.debug("Stuck case detected: %s — %s", case.case_id, case.stuck_reason)


def build_stage_queue(
    cases: List[RadiologyCase],
) -> Dict[str, List[RadiologyCase]]:
    """Group active (non-COMPLETE) cases by their current stage."""
    queue: Dict[str, List[RadiologyCase]] = {s: [] for s in WORKFLOW_STAGES}
    for case in cases:
        stage = case.current_stage or "ORDER"
        if stage in queue:
            queue[stage].append(case)
    return queue


def stage_queue_depth(cases: List[RadiologyCase]) -> Dict[str, int]:
    return {stage: len(lst) for stage, lst in build_stage_queue(cases).items()}
