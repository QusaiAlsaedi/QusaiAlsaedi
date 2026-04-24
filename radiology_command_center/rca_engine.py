"""
RCAEngine — Root Cause Analysis
================================
Automatically identifies the dominant drivers of delay by comparing
subgroup TATs against the overall baseline.

Analysis dimensions
-------------------
  • modality          (CT / MRI / US / XR / NM …)
  • priority          (STAT / URGENT / ROUTINE)
  • time_of_day       (Early Morning / Morning / Afternoon / Evening / Night)
  • day_of_week       (Monday … Sunday)
  • reading_radiologist
  • technologist
  • procedure         (procedure name / type)
  • workflow_stage    (which stage is the biggest bottleneck)

For each dimension × value we compute:
  deviation_pct = (subgroup_avg_tat − baseline_avg_tat) / baseline_avg_tat × 100
  contribution   = (subgroup_excess_minutes × n) / total_excess_minutes

Findings are ranked by contribution score (highest first).
A "recommendation" narrative is generated for every finding above the
minimum deviation threshold.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from statistics import mean, median
from typing import Dict, List, Optional, Tuple

from radiology_command_center.config import STAGE_TRANSITIONS, TOTAL_SLA_MINUTES
from radiology_command_center.models import RadiologyCase, RCAFinding, WorkflowBottleneck

logger = logging.getLogger(__name__)

_MIN_CASES_FOR_FINDING = 2        # ignore subgroups with fewer cases
_MIN_DEVIATION_PCT     = 15.0     # only surface findings with ≥15% deviation
_TIME_BANDS = [
    (0,  5,  "Early Morning"),
    (5,  9,  "Pre-Morning"),
    (9,  12, "Morning"),
    (12, 14, "Midday"),
    (14, 17, "Afternoon"),
    (17, 20, "Evening"),
    (20, 24, "Night"),
]


def _time_of_day_band(dt) -> str:
    if dt is None:
        return "Unknown"
    h = dt.hour
    for start, end, label in _TIME_BANDS:
        if start <= h < end:
            return label
    return "Night"


def _day_of_week(dt) -> str:
    if dt is None:
        return "Unknown"
    return dt.strftime("%A")


def _tat(case: RadiologyCase) -> Optional[float]:
    """Total TAT in minutes for a completed case, else None."""
    if case.order_time and case.report_complete_time:
        return max(0.0, (case.report_complete_time - case.order_time).total_seconds() / 60)
    return case.total_elapsed_minutes if case.total_elapsed_minutes > 0 else None


def _sla_for(case: RadiologyCase) -> float:
    return float(TOTAL_SLA_MINUTES.get(case.priority_norm, 1440))


# ── Core grouping helper ─────────────────────────────────────────────────────

def _group_analysis(
    cases: List[RadiologyCase],
    key_fn,
    baseline_avg: float,
    total_excess: float,
    label: str,
) -> List[RCAFinding]:
    """
    Group cases by key_fn(case), compute per-group TAT stats,
    and return RCAFinding objects for groups above the deviation threshold.
    """
    groups: Dict[str, List[float]] = defaultdict(list)
    for c in cases:
        t = _tat(c)
        if t is not None:
            groups[str(key_fn(c) or "Unknown")].append(t)

    findings: List[RCAFinding] = []
    for value, tats in groups.items():
        if len(tats) < _MIN_CASES_FOR_FINDING:
            continue
        group_avg = mean(tats)
        deviation_pct = ((group_avg - baseline_avg) / baseline_avg * 100) if baseline_avg > 0 else 0.0
        if abs(deviation_pct) < _MIN_DEVIATION_PCT:
            continue

        excess_per_case = max(0.0, group_avg - baseline_avg)
        group_excess = excess_per_case * len(tats)
        contribution = (group_excess / total_excess) if total_excess > 0 else 0.0

        finding = RCAFinding(
            category=label,
            value=value,
            avg_tat_minutes=round(group_avg, 1),
            baseline_tat_minutes=round(baseline_avg, 1),
            deviation_percent=round(deviation_pct, 1),
            affected_cases=len(tats),
            contribution_score=round(contribution, 4),
            recommendation=_make_recommendation(label, value, deviation_pct, group_avg, len(tats)),
        )
        findings.append(finding)

    return findings


def _make_recommendation(
    category: str,
    value: str,
    deviation_pct: float,
    avg_tat: float,
    n: int,
) -> str:
    direction = "slower" if deviation_pct > 0 else "faster"
    pct_abs = abs(deviation_pct)

    templates: Dict[str, str] = {
        "modality": (
            f"{value} exams are {pct_abs:.0f}% {direction} than average "
            f"({avg_tat:.0f} min across {n} cases). "
            + (f"Review {value} scanner throughput, contrast prep, or reporting backlog."
               if direction == "slower" else
               f"Document {value} workflow as a best-practice model.")
        ),
        "priority": (
            f"{value} cases take {pct_abs:.0f}% {direction} than average. "
            + (f"Audit {value} queue handling and escalation adherence."
               if direction == "slower" else "")
        ),
        "time_of_day": (
            f"Cases ordered during {value} are {pct_abs:.0f}% {direction}. "
            + ("Consider adding staffing or staggering exam starts during this period."
               if direction == "slower" else "")
        ),
        "day_of_week": (
            f"{value} shows {pct_abs:.0f}% {direction} TAT. "
            + ("Review Monday/Friday staffing levels or weekend on-call coverage."
               if direction == "slower" else "")
        ),
        "reading_radiologist": (
            f"Dr. {value} reporting TAT is {pct_abs:.0f}% {direction} than peers "
            f"({avg_tat:.0f} min, {n} cases). "
            + ("Discuss workload balance, dictation tooling, or case complexity mix."
               if direction == "slower" else
               "Share workflow patterns with the team.")
        ),
        "technologist": (
            f"Tech {value} has {pct_abs:.0f}% {direction} exam TAT "
            f"({avg_tat:.0f} min, {n} cases). "
            + ("Review positioning time, patient prep, or complex case assignment."
               if direction == "slower" else "")
        ),
        "procedure": (
            f"Procedure '{value}' takes {pct_abs:.0f}% {direction} than average "
            f"({avg_tat:.0f} min). "
            + ("Evaluate protocol efficiency, contrast/sedation logistics."
               if direction == "slower" else "")
        ),
        "workflow_stage": (
            f"Stage '{value}' is absorbing {pct_abs:.0f}% more time than baseline. "
            + ("Identify staffing gaps, approval bottlenecks, or system delays at this stage."
               if direction == "slower" else "")
        ),
    }

    return templates.get(category, f"{category} '{value}': {pct_abs:.0f}% {direction} than average.")


# ── Bottleneck detection ─────────────────────────────────────────────────────

def detect_bottlenecks(
    cases: List[RadiologyCase],
    stage_baselines: Optional[Dict[str, float]] = None,
) -> List[WorkflowBottleneck]:
    """
    Identify stages where the median in-flight duration is significantly
    above expected, using all active cases.
    """
    from radiology_command_center.config import STAGE_MAX_MINUTES

    stage_times: Dict[str, List[float]] = defaultdict(list)
    for c in cases:
        if c.current_stage and c.current_stage not in ("COMPLETE",):
            stage_times[c.current_stage].append(c.time_in_stage_minutes)

    bottlenecks: List[WorkflowBottleneck] = []
    for stage, times in stage_times.items():
        if not times:
            continue
        avg_time = mean(times)
        expected = float(
            STAGE_MAX_MINUTES.get("ROUTINE", {}).get(
                f"{stage}_TO_*", 60  # rough fallback
            )
        )
        # Use the URGENT threshold as "expected" for bottleneck comparison
        urgent_max = STAGE_MAX_MINUTES.get("URGENT", {})
        for _, _, label in STAGE_TRANSITIONS:
            if label.startswith(stage + "_TO_"):
                expected = float(urgent_max.get(label, 60))
                break

        delay = max(0.0, avg_time - expected)
        n_stuck = sum(1 for t in times if t > expected)

        if n_stuck == 0 and delay < 10:
            continue

        severity = (
            "CRITICAL" if delay > expected * 1.5
            else "HIGH" if delay > expected * 0.75
            else "MEDIUM" if delay > expected * 0.25
            else "LOW"
        )

        factors = []
        if n_stuck > 0:
            factors.append(f"{n_stuck} cases stuck at this stage")
        if len(times) > 5:
            factors.append(f"high queue depth ({len(times)} cases)")

        bottlenecks.append(WorkflowBottleneck(
            stage=stage,
            affected_cases=len(times),
            avg_delay_minutes=round(delay, 1),
            severity=severity,
            contributing_factors=factors,
            recommendation=_make_recommendation(
                "workflow_stage", stage, delay / expected * 100 if expected > 0 else 0,
                avg_time, len(times)
            ),
        ))

    bottlenecks.sort(key=lambda b: b.avg_delay_minutes, reverse=True)
    return bottlenecks


# ── Public API ───────────────────────────────────────────────────────────────

class RCAEngine:
    """
    Runs multi-dimensional root cause analysis over a set of cases.

    Pass both historical (completed) cases and active cases for the
    most complete picture.  The engine works on active cases alone if
    historical data is unavailable.
    """

    def analyse(
        self,
        cases: List[RadiologyCase],
        historical: Optional[List[RadiologyCase]] = None,
    ) -> Tuple[List[RCAFinding], List[WorkflowBottleneck]]:
        """
        Returns (rca_findings_sorted_by_contribution, bottlenecks).
        """
        analysis_pool = (historical or []) + cases

        # Only cases with a measurable TAT contribute to RCA
        measurable = [c for c in analysis_pool if _tat(c) is not None]
        if not measurable:
            logger.warning("RCA: no cases with measurable TAT available.")
            return [], detect_bottlenecks(cases)

        all_tats = [_tat(c) for c in measurable]
        baseline_avg = mean(all_tats)
        total_excess = sum(max(0.0, t - baseline_avg) for t in all_tats)

        if total_excess == 0:
            logger.info("RCA: no excess delay detected; all cases at or below baseline.")

        findings: List[RCAFinding] = []

        dimensions = [
            ("modality",          lambda c: c.modality_norm),
            ("priority",          lambda c: c.priority_norm),
            ("time_of_day",       lambda c: _time_of_day_band(c.order_time)),
            ("day_of_week",       lambda c: _day_of_week(c.order_time)),
            ("reading_radiologist",lambda c: c.reading_radiologist),
            ("technologist",      lambda c: c.technologist),
            ("procedure",         lambda c: c.procedure),
        ]

        for label, key_fn in dimensions:
            findings.extend(
                _group_analysis(measurable, key_fn, baseline_avg, total_excess, label)
            )

        # Sort by contribution (descending), then deviation
        findings.sort(key=lambda f: (f.contribution_score, abs(f.deviation_percent)), reverse=True)

        bottlenecks = detect_bottlenecks(cases)

        logger.info(
            "RCA complete: %d findings, %d bottlenecks (baseline avg TAT %.0f min)",
            len(findings), len(bottlenecks), baseline_avg,
        )
        return findings, bottlenecks

    def top_drivers(
        self,
        findings: List[RCAFinding],
        n: int = 5,
    ) -> List[RCAFinding]:
        """Return the top-n findings by contribution score."""
        return sorted(findings, key=lambda f: f.contribution_score, reverse=True)[:n]
