"""Domain models for the Radiology Command Center."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


# ── Core case record ─────────────────────────────────────────────────────────

@dataclass
class RadiologyCase:
    """Single radiology order tracked through the full BESTCare workflow."""

    # Identity
    case_id: str
    mrn: str
    patient_name: str
    age: int
    gender: str

    # Order details
    modality: str               # CT, MRI, US, XR, NM, FLUORO, MAMMO, DEXA
    procedure: str
    priority: str               # STAT | URGENT | ROUTINE
    workflow_type: str          # walk-in | scheduled
    ordering_physician: str
    reading_radiologist: Optional[str] = None
    technologist: Optional[str] = None
    room: Optional[str] = None
    notes: Optional[str] = None

    # Workflow timestamps (None = stage not yet reached)
    order_time: Optional[datetime] = None
    protocol_start_time: Optional[datetime] = None
    protocol_complete_time: Optional[datetime] = None
    tech_confirm_time: Optional[datetime] = None
    checkin_time: Optional[datetime] = None
    worklist_time: Optional[datetime] = None
    procedure_start_time: Optional[datetime] = None
    procedure_complete_time: Optional[datetime] = None
    orr_time: Optional[datetime] = None
    report_start_time: Optional[datetime] = None
    report_complete_time: Optional[datetime] = None

    # Computed by WorkflowTracker
    current_stage: Optional[str] = None
    time_in_stage_minutes: float = 0.0
    total_elapsed_minutes: float = 0.0
    sla_remaining_minutes: float = 0.0
    breach_risk_score: float = 0.0      # 0.0 – 1.0
    priority_score: float = 0.0         # higher = serve sooner
    is_stuck: bool = False
    stuck_reason: Optional[str] = None

    # Per-transition durations (populated by WorkflowTracker)
    stage_durations: Dict[str, float] = field(default_factory=dict)

    @property
    def priority_norm(self) -> str:
        """Return uppercase priority, defaulting to ROUTINE for unknown values."""
        p = (self.priority or "ROUTINE").upper().strip()
        return p if p in ("STAT", "URGENT", "ROUTINE") else "ROUTINE"

    @property
    def modality_norm(self) -> str:
        return (self.modality or "XR").upper().strip()

    @property
    def sla_total_minutes(self) -> int:
        from radiology_command_center.config import TOTAL_SLA_MINUTES
        return TOTAL_SLA_MINUTES.get(self.priority_norm, 1440)

    @property
    def risk_band(self) -> str:
        from radiology_command_center.config import RISK_BAND
        s = self.breach_risk_score
        if s >= RISK_BAND["CRITICAL"]:
            return "CRITICAL"
        if s >= RISK_BAND["HIGH"]:
            return "HIGH"
        if s >= RISK_BAND["MEDIUM"]:
            return "MEDIUM"
        if s >= RISK_BAND["LOW"]:
            return "LOW"
        return "INFO"


# ── Alert ────────────────────────────────────────────────────────────────────

@dataclass
class Alert:
    alert_id: str
    case_id: str
    severity: str               # CRITICAL | HIGH | MEDIUM | LOW | INFO
    category: str               # STUCK_CASE | SLA_BREACH_IMMINENT | etc.
    message: str
    recommended_action: str
    timestamp: datetime

    # Contextual snapshot
    patient_name: str
    modality: str
    priority: str
    current_stage: str
    time_in_stage_minutes: float
    sla_remaining_minutes: float

    is_acknowledged: bool = False


# ── Bottleneck / RCA findings ────────────────────────────────────────────────

@dataclass
class WorkflowBottleneck:
    stage: str
    affected_cases: int
    avg_delay_minutes: float
    severity: str               # CRITICAL | HIGH | MEDIUM | LOW
    contributing_factors: List[str] = field(default_factory=list)
    recommendation: str = ""


@dataclass
class RCAFinding:
    category: str               # modality | time_of_day | day_of_week | technologist | radiologist
    value: str                  # e.g. "MRI", "Morning", "Monday"
    avg_tat_minutes: float
    baseline_tat_minutes: float
    deviation_percent: float    # positive = slower than baseline
    affected_cases: int
    contribution_score: float   # 0–1, share of total excess delay
    recommendation: str = ""


# ── Report containers ────────────────────────────────────────────────────────

@dataclass
class DailyMetrics:
    report_date: datetime
    total_cases: int
    completed_cases: int
    in_progress_cases: int
    stat_cases: int
    urgent_cases: int
    routine_cases: int
    sla_compliant: int
    sla_breaches: int
    at_risk_cases: int              # high/critical risk but not yet breached
    avg_tat_minutes: float
    median_tat_minutes: float
    avg_tat_by_modality: Dict[str, float] = field(default_factory=dict)
    avg_tat_by_priority: Dict[str, float] = field(default_factory=dict)
    avg_stage_duration: Dict[str, float] = field(default_factory=dict)
    throughput_by_hour: Dict[int, int] = field(default_factory=dict)
    cases_per_modality: Dict[str, int] = field(default_factory=dict)


@dataclass
class OperationalReport:
    metrics: DailyMetrics
    bottlenecks: List[WorkflowBottleneck]
    rca_findings: List[RCAFinding]
    active_alerts: List[Alert]
    prioritized_worklist: List[RadiologyCase]
    recommendations: List[str]
    executive_summary: str
