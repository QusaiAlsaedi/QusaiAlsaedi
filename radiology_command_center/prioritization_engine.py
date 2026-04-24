"""
PrioritizationEngine
====================
Assigns every active case a composite priority_score and returns a
ranked worklist with a "next best case" recommendation.

Scoring formula (all terms are non-negative, higher = serve sooner)
----------------------------------------------------------------------
  score = priority_base
        + breach_urgency_bonus
        + wait_time_bonus
        + escalation_bonus
        - modality_queue_penalty

priority_base        : STAT=1000, URGENT=500, ROUTINE=100
breach_urgency_bonus : breach_risk × 600
                       (a ROUTINE at 90% risk outscores an URGENT at 10% risk)
wait_time_bonus      : minutes_since_order / 5
                       (adds 1 point per 5 min of waiting — prevents starvation)
escalation_bonus     : +200 if case is stuck; +100 if SLA remaining ≤ 20 min
modality_queue_penalty: 0–50, reduces score when many same-modality cases
                       compete (prevents one modality from monopolising the list)

The recommendation narrative explains *why* the top case was chosen.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Dict, List, Optional, Tuple

from radiology_command_center.config import PRIORITY_BASE_SCORE
from radiology_command_center.models import RadiologyCase

logger = logging.getLogger(__name__)

_BREACH_URGENCY_WEIGHT    = 600.0
_WAIT_WEIGHT              = 0.2        # points per minute of waiting
_STUCK_BONUS              = 200.0
_SLA_IMMINENT_BONUS       = 100.0      # when SLA remaining ≤ 20 min
_SLA_IMMINENT_THRESHOLD   = 20.0       # minutes
_QUEUE_PENALTY_MAX        = 50.0       # max penalty for same-modality depth


def _modality_queue_penalty(case: RadiologyCase, queue_depth: Dict[str, int]) -> float:
    """
    Slight penalty when many cases of the same modality are ahead,
    to encourage inter-modality fairness on a shared worklist.
    The penalty never exceeds _QUEUE_PENALTY_MAX.
    """
    depth = queue_depth.get(case.modality_norm, 1)
    # penalty grows as log(depth), capped
    import math
    penalty = min(_QUEUE_PENALTY_MAX, 10.0 * math.log1p(max(0, depth - 1)))
    return penalty


def score_case(
    case: RadiologyCase,
    queue_depth: Dict[str, int],
) -> float:
    """Compute and return the priority score for a single case."""
    # Base from priority
    score = float(PRIORITY_BASE_SCORE.get(case.priority_norm, 100))

    # SLA breach risk drives urgency
    score += case.breach_risk_score * _BREACH_URGENCY_WEIGHT

    # Anti-starvation: reward long wait
    score += case.total_elapsed_minutes * _WAIT_WEIGHT

    # Escalation bonuses
    if case.is_stuck:
        score += _STUCK_BONUS
    if 0 < case.sla_remaining_minutes <= _SLA_IMMINENT_THRESHOLD:
        score += _SLA_IMMINENT_BONUS

    # Fairness penalty
    score -= _modality_queue_penalty(case, queue_depth)

    return round(score, 2)


def build_recommendation(case: RadiologyCase, rank: int) -> str:
    """One-sentence action recommendation for the top-ranked case."""
    reasons = []

    if case.priority_norm == "STAT":
        reasons.append("STAT priority")
    elif case.priority_norm == "URGENT":
        reasons.append("URGENT priority")

    if case.is_stuck:
        reasons.append(f"stuck at {case.current_stage} for {case.time_in_stage_minutes:.0f} min")

    risk_pct = int(case.breach_risk_score * 100)
    if risk_pct >= 85:
        reasons.append(f"{risk_pct}% SLA breach probability")
    elif risk_pct >= 45:
        reasons.append(f"{risk_pct}% SLA breach risk")

    if 0 < case.sla_remaining_minutes <= _SLA_IMMINENT_THRESHOLD:
        reasons.append(f"only {case.sla_remaining_minutes:.0f} min SLA remaining")

    if case.total_elapsed_minutes > 120:
        reasons.append(f"waiting {case.total_elapsed_minutes:.0f} min total")

    reason_str = "; ".join(reasons) if reasons else "standard queue order"
    return (
        f"Serve {case.patient_name} ({case.modality_norm} — {case.procedure}) "
        f"next [score {case.priority_score:.0f}] — {reason_str}."
    )


# ── Public API ───────────────────────────────────────────────────────────────

class PrioritizationEngine:
    """
    Ranks active cases and exposes the prioritized worklist.

    Usage::
        engine = PrioritizationEngine()
        ranked = engine.rank(cases)
        recommendation = engine.next_best_case(ranked)
    """

    def rank(self, cases: List[RadiologyCase]) -> List[RadiologyCase]:
        """
        Score and sort cases in-place (highest score first).
        Returns the same list for chaining convenience.
        """
        # Count how many cases share each modality (for queue penalty)
        modality_counts: Dict[str, int] = Counter(
            c.modality_norm for c in cases if c.current_stage not in (None, "COMPLETE")
        )

        for case in cases:
            case.priority_score = score_case(case, modality_counts)

        cases.sort(key=lambda c: c.priority_score, reverse=True)

        logger.info("Prioritized %d cases", len(cases))
        return cases

    def next_best_case(
        self,
        ranked_cases: List[RadiologyCase],
        stage_filter: Optional[str] = None,
    ) -> Optional[Tuple[RadiologyCase, str]]:
        """
        Return (top_case, recommendation_text) for the highest-scored
        case that is actionable right now.

        stage_filter: if provided, only consider cases at this stage
                      (e.g. "WORKLIST" to get the next case to scan).
        """
        candidates = [
            c for c in ranked_cases
            if c.current_stage not in (None, "COMPLETE")
            and (stage_filter is None or c.current_stage == stage_filter)
        ]

        if not candidates:
            return None

        top = candidates[0]
        recommendation = build_recommendation(top, rank=1)
        return top, recommendation

    def worklist_for_stage(
        self,
        ranked_cases: List[RadiologyCase],
        stage: str,
    ) -> List[RadiologyCase]:
        """Return only cases currently at `stage`, in priority order."""
        return [c for c in ranked_cases if c.current_stage == stage]

    def escalation_list(
        self,
        ranked_cases: List[RadiologyCase],
        min_risk: float = 0.65,
    ) -> List[RadiologyCase]:
        """Cases that need immediate attention (high/critical risk)."""
        return [
            c for c in ranked_cases
            if c.breach_risk_score >= min_risk or c.is_stuck
        ]
