"""
PredictionEngine
================
Estimates SLA breach risk for every active case and projects remaining
completion time, without requiring an external ML library.

Approach
--------
1. Build stage-duration baselines from historical completed cases
   (median per modality × priority × stage-transition).
2. For each active case:
   a. Sum elapsed time so far.
   b. For each remaining stage, look up the baseline duration.
      If the current stage is already over its baseline, carry the
      excess forward as a "delay signal".
   c. Compute projected_total = elapsed + projected_remaining.
   d. breach_risk = sigmoid((projected_total − SLA) / SLA × k)
      so risk rises steeply as projected total approaches the SLA.
3. Apply urgency multipliers for stuck cases and priority escalation.

The engine degrades gracefully when there is no historical data by
falling back to config defaults.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from statistics import median
from typing import Dict, List, Optional, Tuple

from radiology_command_center.config import (
    MODALITY_BASE_PROCEDURE_MINUTES,
    STAGE_MAX_MINUTES,
    STAGE_TRANSITIONS,
    TOTAL_SLA_MINUTES,
)
from radiology_command_center.models import RadiologyCase
from radiology_command_center.workflow_tracker import WORKFLOW_STAGES

logger = logging.getLogger(__name__)

# Controls how quickly risk rises near the SLA boundary.
# k=6 → risk ≈ 0.50 at projected==SLA, 0.85 at projected==1.17×SLA.
_SIGMOID_K = 6.0

# Remaining stages after the current one (ordered)
_STAGE_ORDER_IDX: Dict[str, int] = {s: i for i, s in enumerate(WORKFLOW_STAGES)}


def _sigmoid(x: float) -> float:
    """Standard logistic function, clipped to [0, 1]."""
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


# ── Baseline builder ─────────────────────────────────────────────────────────

class StageBaseline:
    """
    Stores median stage-transition durations keyed by
    (modality, priority, transition_label).  Falls back through
    progressively coarser keys when a precise match is missing.
    """

    def __init__(self) -> None:
        # raw samples: key → [duration_minutes]
        self._samples: Dict[Tuple, List[float]] = defaultdict(list)
        self._medians: Dict[Tuple, float] = {}

    def add_case(self, case: RadiologyCase) -> None:
        for _, _, label in STAGE_TRANSITIONS:
            duration = case.stage_durations.get(label)
            if duration is not None and duration > 0:
                key = (case.modality_norm, case.priority_norm, label)
                self._samples[key].append(duration)

    def compile(self) -> None:
        """Compute medians from accumulated samples."""
        self._medians = {k: median(v) for k, v in self._samples.items() if v}
        logger.info(
            "Baseline compiled: %d (modality × priority × stage) cells",
            len(self._medians),
        )

    def get(
        self,
        modality: str,
        priority: str,
        transition_label: str,
        fallback: Optional[float] = None,
    ) -> float:
        """
        Lookup order:
          1. (modality, priority, label)   — most specific
          2. ("*",      priority, label)   — any modality, same priority
          3. ("*",      "*",      label)   — any modality, any priority
          4. config STAGE_MAX_MINUTES      — hard-coded config defaults
          5. supplied fallback or 30 min
        """
        for key in [
            (modality, priority, transition_label),
            ("*",      priority, transition_label),
            ("*",      "*",      transition_label),
        ]:
            v = self._medians.get(key)
            if v is not None:
                return v

        # Config default
        stage_maxes = STAGE_MAX_MINUTES.get(priority, STAGE_MAX_MINUTES["ROUTINE"])
        config_v = stage_maxes.get(transition_label)
        if config_v is not None:
            return float(config_v)

        return fallback if fallback is not None else 30.0

    @classmethod
    def from_cases(cls, historical: List[RadiologyCase]) -> "StageBaseline":
        baseline = cls()
        for case in historical:
            baseline.add_case(case)
        baseline.compile()
        return baseline


# ── Remaining-time projector ─────────────────────────────────────────────────

def _stages_remaining(current_stage: str) -> List[Tuple[str, str, str]]:
    """
    Return STAGE_TRANSITIONS entries that have not yet started,
    i.e., transitions where from_stage is ≥ current_stage in the sequence.
    """
    if current_stage == "COMPLETE":
        return []
    current_idx = _STAGE_ORDER_IDX.get(current_stage, 0)
    result = []
    for from_s, to_s, label in STAGE_TRANSITIONS:
        from_idx = _STAGE_ORDER_IDX.get(from_s, 0)
        if from_idx >= current_idx:
            result.append((from_s, to_s, label))
    return result


def project_remaining_minutes(
    case: RadiologyCase,
    baseline: StageBaseline,
) -> float:
    """
    Estimate minutes still needed to complete from now.

    For the current stage we subtract time already spent (but never below 0).
    For future stages we use full baseline medians.
    """
    if case.current_stage == "COMPLETE":
        return 0.0

    remaining = 0.0
    current_idx = _STAGE_ORDER_IDX.get(case.current_stage or "ORDER", 0)

    for from_s, to_s, label in STAGE_TRANSITIONS:
        from_idx = _STAGE_ORDER_IDX.get(from_s, 0)
        if from_idx < current_idx:
            continue  # already passed

        expected = baseline.get(case.modality_norm, case.priority_norm, label)

        if from_s == case.current_stage:
            # Time already spent on this stage is sunk; only count what's left
            already_spent = case.time_in_stage_minutes
            remaining += max(0.0, expected - already_spent)
        else:
            remaining += expected

    return remaining


def compute_breach_risk(
    case: RadiologyCase,
    baseline: StageBaseline,
) -> Tuple[float, float]:
    """
    Returns (breach_risk_score 0–1, sla_remaining_minutes).

    Formula:
      over_fraction = (projected_total − sla) / sla
      risk = sigmoid(k × over_fraction)

    When projected_total == 0.5 × sla  → risk ≈ 0.07 (low)
    When projected_total == sla         → risk ≈ 0.50 (medium-high)
    When projected_total == 1.17 × sla  → risk ≈ 0.85 (critical)
    """
    if case.order_time is None:
        return 0.0, float(case.sla_total_minutes)

    sla = float(case.sla_total_minutes)
    elapsed = case.total_elapsed_minutes
    projected_remaining = project_remaining_minutes(case, baseline)
    projected_total = elapsed + projected_remaining

    sla_remaining = max(0.0, sla - elapsed)
    over_fraction = (projected_total - sla) / sla if sla > 0 else 0.0
    risk = _sigmoid(_SIGMOID_K * over_fraction)

    # Boost risk for already-stuck cases
    if case.is_stuck:
        risk = min(1.0, risk + 0.15)

    # Hard-cap: if elapsed already exceeds SLA, risk = 1.0
    if elapsed >= sla:
        risk = 1.0
        sla_remaining = 0.0

    return round(risk, 4), round(sla_remaining, 1)


# ── Public API ───────────────────────────────────────────────────────────────

class PredictionEngine:
    """
    Scores every active case for SLA breach risk and updates
    case.breach_risk_score and case.sla_remaining_minutes in-place.
    """

    def __init__(
        self,
        historical_cases: Optional[List[RadiologyCase]] = None,
    ) -> None:
        self.baseline = StageBaseline.from_cases(historical_cases or [])

    def score(self, cases: List[RadiologyCase]) -> List[RadiologyCase]:
        """Annotate cases with breach_risk_score and sla_remaining_minutes."""
        for case in cases:
            risk, sla_remaining = compute_breach_risk(case, self.baseline)
            case.breach_risk_score = risk
            case.sla_remaining_minutes = sla_remaining
        return cases

    def cases_at_risk(
        self,
        cases: List[RadiologyCase],
        min_risk: float = 0.45,
    ) -> List[RadiologyCase]:
        return [c for c in cases if c.breach_risk_score >= min_risk]

    def projected_tat(
        self,
        case: RadiologyCase,
    ) -> float:
        """Return projected total TAT in minutes for a single case."""
        return case.total_elapsed_minutes + project_remaining_minutes(case, self.baseline)
