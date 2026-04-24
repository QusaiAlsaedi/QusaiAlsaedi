"""
ReportGenerator
===============
Produces three outputs from a fully-enriched case set:

  1. Real-time Alert log    — immediate action items (Excel + in-memory)
  2. Daily Operational Report — metrics, RCA, bottlenecks, worklist (Excel)
  3. Executive Summary       — narrative text for leadership (plain text file)

All outputs are also returned as Python objects so callers can
post-process or display them as needed.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime
from statistics import mean, median
from typing import Dict, List, Optional, Tuple

import pandas as pd

from radiology_command_center.config import (
    ALERT_CATEGORIES,
    REPORT_OUTPUT_DIR,
    RISK_BAND,
    TOTAL_SLA_MINUTES,
)
from radiology_command_center.data_loader import cases_to_dataframe, write_excel_report
from radiology_command_center.models import (
    Alert,
    DailyMetrics,
    OperationalReport,
    RCAFinding,
    RadiologyCase,
    WorkflowBottleneck,
)
from radiology_command_center.workflow_tracker import WORKFLOW_STAGES

logger = logging.getLogger(__name__)


# ── Alert generation ─────────────────────────────────────────────────────────

def _make_alert(
    case: RadiologyCase,
    severity: str,
    category: str,
    message: str,
    action: str,
    now: datetime,
) -> Alert:
    return Alert(
        alert_id=str(uuid.uuid4())[:8].upper(),
        case_id=case.case_id,
        severity=severity,
        category=category,
        message=message,
        recommended_action=action,
        timestamp=now,
        patient_name=case.patient_name,
        modality=case.modality_norm,
        priority=case.priority_norm,
        current_stage=case.current_stage or "UNKNOWN",
        time_in_stage_minutes=case.time_in_stage_minutes,
        sla_remaining_minutes=case.sla_remaining_minutes,
    )


def generate_alerts(
    cases: List[RadiologyCase],
    now: Optional[datetime] = None,
) -> List[Alert]:
    """
    Inspect every case and emit alerts for:
      - Stuck cases
      - SLA already breached
      - SLA breach imminent (high/critical risk)
    """
    if now is None:
        now = datetime.now()

    alerts: List[Alert] = []

    for case in cases:
        if case.current_stage == "COMPLETE":
            continue

        # ── SLA already breached ────────────────────────────────────────────
        if case.breach_risk_score >= 1.0 or case.sla_remaining_minutes == 0:
            alerts.append(_make_alert(
                case, "CRITICAL", "SLA_BREACHED",
                message=(
                    f"{case.priority_norm} {case.modality_norm} for {case.patient_name} "
                    f"(ID {case.case_id}) has exceeded SLA by "
                    f"{abs(case.sla_remaining_minutes):.0f} min. "
                    f"Currently at {case.current_stage}."
                ),
                action=(
                    "Escalate immediately to charge radiologist and department manager. "
                    "Assign next available reader/technologist."
                ),
                now=now,
            ))
            continue  # one alert per case max

        # ── SLA breach imminent ─────────────────────────────────────────────
        if case.breach_risk_score >= RISK_BAND["CRITICAL"]:
            alerts.append(_make_alert(
                case, "CRITICAL", "SLA_BREACH_IMMINENT",
                message=(
                    f"{case.priority_norm} {case.modality_norm} for {case.patient_name} "
                    f"(ID {case.case_id}): {int(case.breach_risk_score * 100)}% SLA breach risk. "
                    f"Stage: {case.current_stage}. SLA remaining: {case.sla_remaining_minutes:.0f} min."
                ),
                action=(
                    "Prioritize immediately. Check for stuck transitions and resource availability."
                ),
                now=now,
            ))
        elif case.breach_risk_score >= RISK_BAND["HIGH"]:
            alerts.append(_make_alert(
                case, "HIGH", "SLA_BREACH_IMMINENT",
                message=(
                    f"{case.priority_norm} {case.modality_norm} (ID {case.case_id}): "
                    f"HIGH breach risk ({int(case.breach_risk_score * 100)}%). "
                    f"Stage: {case.current_stage}."
                ),
                action="Move to top of modality queue. Notify assigned technologist.",
                now=now,
            ))

        # ── Stuck case (may overlap with breach risk but distinct action) ───
        if case.is_stuck and case.stuck_reason:
            severity = "CRITICAL" if case.priority_norm == "STAT" else "HIGH"
            alerts.append(_make_alert(
                case, severity, "STUCK_CASE",
                message=f"Case {case.case_id} is stuck: {case.stuck_reason}",
                action=(
                    "Investigate the blocking step. Options: reassign, escalate protocol "
                    "approval, check EMR/RIS connectivity, or notify on-call staff."
                ),
                now=now,
            ))

    # Sort: CRITICAL first, then by SLA remaining ascending
    _rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
    alerts.sort(key=lambda a: (_rank.get(a.severity, 99), a.sla_remaining_minutes))

    logger.info("Generated %d alerts", len(alerts))
    return alerts


# ── Metrics computation ──────────────────────────────────────────────────────

def compute_metrics(
    cases: List[RadiologyCase],
    now: Optional[datetime] = None,
) -> DailyMetrics:
    if now is None:
        now = datetime.now()

    completed    = [c for c in cases if c.current_stage == "COMPLETE"]
    in_progress  = [c for c in cases if c.current_stage not in (None, "COMPLETE")]

    tats = [c.total_elapsed_minutes for c in completed if c.total_elapsed_minutes > 0]
    avg_tat    = round(mean(tats), 1)  if tats else 0.0
    median_tat = round(median(tats), 1) if tats else 0.0

    sla_breaches = sum(
        1 for c in completed
        if c.total_elapsed_minutes > TOTAL_SLA_MINUTES.get(c.priority_norm, 1440)
    )
    sla_compliant = len(completed) - sla_breaches

    at_risk = sum(1 for c in in_progress if c.breach_risk_score >= RISK_BAND["HIGH"])

    # Per-modality average TAT
    mod_tats: Dict[str, List[float]] = defaultdict(list)
    for c in completed:
        if c.total_elapsed_minutes > 0:
            mod_tats[c.modality_norm].append(c.total_elapsed_minutes)
    avg_tat_by_modality = {m: round(mean(v), 1) for m, v in mod_tats.items()}

    # Per-priority average TAT
    prio_tats: Dict[str, List[float]] = defaultdict(list)
    for c in completed:
        if c.total_elapsed_minutes > 0:
            prio_tats[c.priority_norm].append(c.total_elapsed_minutes)
    avg_tat_by_priority = {p: round(mean(v), 1) for p, v in prio_tats.items()}

    # Average stage duration across all cases
    stage_dur: Dict[str, List[float]] = defaultdict(list)
    for c in cases:
        for label, mins in c.stage_durations.items():
            stage_dur[label].append(mins)
    avg_stage_duration = {l: round(mean(v), 1) for l, v in stage_dur.items()}

    # Throughput by hour (completions)
    hourly: Dict[int, int] = defaultdict(int)
    for c in completed:
        if c.report_complete_time:
            hourly[c.report_complete_time.hour] += 1
    throughput_by_hour = dict(sorted(hourly.items()))

    # Cases per modality (all)
    mod_counts: Dict[str, int] = defaultdict(int)
    for c in cases:
        mod_counts[c.modality_norm] += 1
    cases_per_modality = dict(mod_counts)

    return DailyMetrics(
        report_date=now,
        total_cases=len(cases),
        completed_cases=len(completed),
        in_progress_cases=len(in_progress),
        stat_cases=sum(1 for c in cases if c.priority_norm == "STAT"),
        urgent_cases=sum(1 for c in cases if c.priority_norm == "URGENT"),
        routine_cases=sum(1 for c in cases if c.priority_norm == "ROUTINE"),
        sla_compliant=sla_compliant,
        sla_breaches=sla_breaches,
        at_risk_cases=at_risk,
        avg_tat_minutes=avg_tat,
        median_tat_minutes=median_tat,
        avg_tat_by_modality=avg_tat_by_modality,
        avg_tat_by_priority=avg_tat_by_priority,
        avg_stage_duration=avg_stage_duration,
        throughput_by_hour=throughput_by_hour,
        cases_per_modality=cases_per_modality,
    )


# ── Recommendation generator ─────────────────────────────────────────────────

def generate_recommendations(
    metrics: DailyMetrics,
    bottlenecks: List[WorkflowBottleneck],
    rca_findings: List[RCAFinding],
    alerts: List[Alert],
) -> List[str]:
    recs: List[str] = []

    # SLA compliance
    total = metrics.completed_cases
    if total > 0:
        breach_pct = metrics.sla_breaches / total * 100
        if breach_pct >= 20:
            recs.append(
                f"URGENT: {breach_pct:.0f}% of completed cases breached SLA today "
                f"({metrics.sla_breaches}/{total}). Convene a same-day ops review."
            )
        elif breach_pct >= 10:
            recs.append(
                f"SLA breach rate is {breach_pct:.0f}% — above target. "
                "Review top bottleneck stages and adjust staffing for the next shift."
            )

    # At-risk
    if metrics.at_risk_cases > 0:
        recs.append(
            f"{metrics.at_risk_cases} in-progress case(s) at HIGH/CRITICAL breach risk. "
            "Assign dedicated resource to clear these immediately."
        )

    # Top bottleneck
    if bottlenecks:
        b = bottlenecks[0]
        recs.append(
            f"Primary bottleneck: {b.stage} stage "
            f"(avg {b.avg_delay_minutes:.0f} min above expected, {b.affected_cases} cases). "
            f"{b.recommendation}"
        )

    # Top RCA finding
    if rca_findings:
        f = rca_findings[0]
        recs.append(f"Top delay driver: {f.recommendation}")

    # STAT/URGENT SLA
    stat_tat = metrics.avg_tat_by_priority.get("STAT")
    stat_sla = TOTAL_SLA_MINUTES["STAT"]
    if stat_tat and stat_tat > stat_sla:
        recs.append(
            f"STAT average TAT ({stat_tat:.0f} min) exceeds the {stat_sla}-min SLA. "
            "Audit STAT workflow path for unnecessary handoff delays."
        )

    # Critical alerts
    critical_count = sum(1 for a in alerts if a.severity == "CRITICAL")
    if critical_count > 0:
        recs.append(
            f"{critical_count} CRITICAL alert(s) require immediate action. "
            "See the Alerts sheet for details."
        )

    if not recs:
        recs.append("Operations are within normal parameters. No immediate actions required.")

    return recs


# ── Executive summary ────────────────────────────────────────────────────────

def build_executive_summary(
    metrics: DailyMetrics,
    bottlenecks: List[WorkflowBottleneck],
    rca_findings: List[RCAFinding],
    alerts: List[Alert],
    recommendations: List[str],
    prioritized_worklist: List[RadiologyCase],
) -> str:
    now_str = metrics.report_date.strftime("%A, %B %d %Y  %H:%M")
    sla_pct = (
        metrics.sla_compliant / metrics.completed_cases * 100
        if metrics.completed_cases > 0 else 0
    )

    critical_alerts = [a for a in alerts if a.severity == "CRITICAL"]
    high_alerts     = [a for a in alerts if a.severity == "HIGH"]

    next_case_line = ""
    if prioritized_worklist:
        top = prioritized_worklist[0]
        next_case_line = (
            f"\n  NEXT BEST CASE: {top.patient_name} | {top.modality_norm} "
            f"| {top.priority_norm} | Score {top.priority_score:.0f} "
            f"| Stage {top.current_stage} | Risk {int(top.breach_risk_score * 100)}%"
        )

    top_drivers = rca_findings[:3]
    driver_lines = "\n".join(
        f"  {i+1}. [{f.category.upper()}] {f.value}: "
        f"{f.deviation_percent:+.0f}% vs baseline, {f.affected_cases} cases — {f.recommendation}"
        for i, f in enumerate(top_drivers)
    ) or "  No significant delay drivers identified."

    rec_lines = "\n".join(f"  • {r}" for r in recommendations)

    bottleneck_lines = "\n".join(
        f"  [{b.severity}] {b.stage}: +{b.avg_delay_minutes:.0f} min avg delay, "
        f"{b.affected_cases} cases — {b.recommendation}"
        for b in bottlenecks[:3]
    ) or "  No significant bottlenecks detected."

    summary = f"""
╔══════════════════════════════════════════════════════════════════════════════╗
║           RADIOLOGY COMMAND CENTER — EXECUTIVE SUMMARY                     ║
╚══════════════════════════════════════════════════════════════════════════════╝
  Generated : {now_str}

──────────────────────────────────────────────────────────────────────────────
  OPERATIONAL SNAPSHOT
──────────────────────────────────────────────────────────────────────────────
  Total Cases     : {metrics.total_cases}
  Completed       : {metrics.completed_cases}   (SLA compliance {sla_pct:.1f}%)
  In Progress     : {metrics.in_progress_cases}
  SLA Breaches    : {metrics.sla_breaches}
  At Risk (high+) : {metrics.at_risk_cases}

  STAT  : {metrics.stat_cases} cases  │  URGENT : {metrics.urgent_cases} cases  │  ROUTINE : {metrics.routine_cases} cases

  Avg TAT         : {metrics.avg_tat_minutes:.0f} min  (median {metrics.median_tat_minutes:.0f} min)
  Avg TAT by Priority : {', '.join(f"{p} {v:.0f}m" for p, v in sorted(metrics.avg_tat_by_priority.items())) or "N/A"}
  Avg TAT by Modality : {', '.join(f"{m} {v:.0f}m" for m, v in sorted(metrics.avg_tat_by_modality.items())) or "N/A"}

──────────────────────────────────────────────────────────────────────────────
  ALERTS  ( {len(critical_alerts)} CRITICAL  |  {len(high_alerts)} HIGH  |  {len(alerts)} total )
──────────────────────────────────────────────────────────────────────────────
{chr(10).join(f"  [{a.severity}] {a.message}" for a in alerts[:5]) or "  No active alerts."}
{"  ... (see Alerts sheet for full list)" if len(alerts) > 5 else ""}

──────────────────────────────────────────────────────────────────────────────
  WORKFLOW BOTTLENECKS
──────────────────────────────────────────────────────────────────────────────
{bottleneck_lines}

──────────────────────────────────────────────────────────────────────────────
  ROOT CAUSE ANALYSIS — TOP DELAY DRIVERS
──────────────────────────────────────────────────────────────────────────────
{driver_lines}

──────────────────────────────────────────────────────────────────────────────
  WORKLIST PRIORITY{next_case_line}
──────────────────────────────────────────────────────────────────────────────
  Top 5 cases to serve next:
{chr(10).join(f"  {i+1:2}. [{c.priority_norm:7s}] {c.modality_norm:5s} {c.patient_name:<20s} Stage:{c.current_stage:<12s} Risk:{int(c.breach_risk_score*100):3d}%  Score:{c.priority_score:7.0f}" for i, c in enumerate(prioritized_worklist[:5])) or "  No active worklist cases."}

──────────────────────────────────────────────────────────────────────────────
  RECOMMENDATIONS
──────────────────────────────────────────────────────────────────────────────
{rec_lines}

══════════════════════════════════════════════════════════════════════════════
""".lstrip("\n")

    return summary


# ── Excel report sheets ──────────────────────────────────────────────────────

def _alerts_to_df(alerts: List[Alert]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Alert ID":           a.alert_id,
        "Timestamp":          a.timestamp.strftime("%Y-%m-%d %H:%M"),
        "Severity":           a.severity,
        "Category":           a.category,
        "Case ID":            a.case_id,
        "Patient":            a.patient_name,
        "Modality":           a.modality,
        "Priority":           a.priority,
        "Stage":              a.current_stage,
        "Time in Stage (min)":round(a.time_in_stage_minutes, 1),
        "SLA Remaining (min)":round(a.sla_remaining_minutes, 1),
        "Message":            a.message,
        "Recommended Action": a.recommended_action,
    } for a in alerts])


def _metrics_to_df(m: DailyMetrics) -> pd.DataFrame:
    rows = [
        ("Report Date",           m.report_date.strftime("%Y-%m-%d %H:%M")),
        ("Total Cases",           m.total_cases),
        ("Completed",             m.completed_cases),
        ("In Progress",           m.in_progress_cases),
        ("STAT Cases",            m.stat_cases),
        ("URGENT Cases",          m.urgent_cases),
        ("ROUTINE Cases",         m.routine_cases),
        ("SLA Compliant",         m.sla_compliant),
        ("SLA Breaches",          m.sla_breaches),
        ("At-Risk Cases (High+)", m.at_risk_cases),
        ("Avg TAT (min)",         m.avg_tat_minutes),
        ("Median TAT (min)",      m.median_tat_minutes),
    ]
    for p, v in sorted(m.avg_tat_by_priority.items()):
        rows.append((f"Avg TAT {p} (min)", v))
    for mod, v in sorted(m.avg_tat_by_modality.items()):
        rows.append((f"Avg TAT {mod} (min)", v))
    return pd.DataFrame(rows, columns=["Metric", "Value"])


def _rca_to_df(findings: List[RCAFinding]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Category":           f.category,
        "Value":              f.value,
        "Avg TAT (min)":      f.avg_tat_minutes,
        "Baseline TAT (min)": f.baseline_tat_minutes,
        "Deviation (%)":      f.deviation_percent,
        "Affected Cases":     f.affected_cases,
        "Contribution Score": round(f.contribution_score, 4),
        "Recommendation":     f.recommendation,
    } for f in findings])


def _bottlenecks_to_df(bottlenecks: List[WorkflowBottleneck]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Stage":              b.stage,
        "Affected Cases":     b.affected_cases,
        "Avg Delay (min)":    b.avg_delay_minutes,
        "Severity":           b.severity,
        "Contributing Factors": "; ".join(b.contributing_factors),
        "Recommendation":     b.recommendation,
    } for b in bottlenecks])


# ── Public API ───────────────────────────────────────────────────────────────

class ReportGenerator:
    """
    Orchestrates all reporting outputs from a fully-enriched set of cases.

    Usage::
        report = ReportGenerator(output_dir="reports").run(cases, historical)
        # report is an OperationalReport object
        # files are written to output_dir/
    """

    def __init__(self, output_dir: str = REPORT_OUTPUT_DIR):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def run(
        self,
        cases: List[RadiologyCase],
        historical: Optional[List[RadiologyCase]] = None,
        rca_findings: Optional[List[RCAFinding]] = None,
        bottlenecks: Optional[List[WorkflowBottleneck]] = None,
        now: Optional[datetime] = None,
    ) -> OperationalReport:
        if now is None:
            now = datetime.now()

        # 1. Alerts
        alerts = generate_alerts(cases, now)

        # 2. Metrics
        metrics = compute_metrics(cases, now)

        # 3. Worklist (already ranked by PrioritizationEngine before calling here)
        prioritized = [c for c in cases if c.current_stage not in (None, "COMPLETE")]
        prioritized.sort(key=lambda c: c.priority_score, reverse=True)

        # 4. Recommendations
        rca = rca_findings or []
        bns = bottlenecks or []
        recommendations = generate_recommendations(metrics, bns, rca, alerts)

        # 5. Executive summary
        exec_summary = build_executive_summary(
            metrics, bns, rca, alerts, recommendations, prioritized
        )

        report = OperationalReport(
            metrics=metrics,
            bottlenecks=bns,
            rca_findings=rca,
            active_alerts=alerts,
            prioritized_worklist=prioritized,
            recommendations=recommendations,
            executive_summary=exec_summary,
        )

        self._write_outputs(report, cases, now)
        return report

    def _write_outputs(
        self,
        report: OperationalReport,
        cases: List[RadiologyCase],
        now: datetime,
    ) -> None:
        date_tag = now.strftime("%Y%m%d_%H%M")

        # ── Daily operational Excel ──────────────────────────────────────────
        daily_path = os.path.join(
            self.output_dir, f"radiology_daily_report_{date_tag}.xlsx"
        )
        worklist_df = cases_to_dataframe(report.prioritized_worklist)
        all_cases_df = cases_to_dataframe(cases)

        write_excel_report(
            daily_path,
            {
                "Summary Metrics":  _metrics_to_df(report.metrics),
                "Prioritized Worklist": worklist_df,
                "All Cases":         all_cases_df,
                "Alerts":            _alerts_to_df(report.active_alerts),
                "Bottlenecks":       _bottlenecks_to_df(report.bottlenecks),
                "RCA Findings":      _rca_to_df(report.rca_findings),
                "Recommendations":   pd.DataFrame(
                    {"Recommendation": report.recommendations}
                ),
            },
        )
        logger.info("Daily report: %s", daily_path)

        # ── Executive summary text ───────────────────────────────────────────
        exec_path = os.path.join(
            self.output_dir, f"executive_summary_{date_tag}.txt"
        )
        with open(exec_path, "w", encoding="utf-8") as fh:
            fh.write(report.executive_summary)
        logger.info("Executive summary: %s", exec_path)

        # ── Alert log (latest, overwritten each run) ─────────────────────────
        alert_path = os.path.join(self.output_dir, "alerts_current.xlsx")
        write_excel_report(
            alert_path,
            {"Alerts": _alerts_to_df(report.active_alerts)},
        )
