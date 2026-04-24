"""
Central configuration: SLA targets, stage thresholds, scoring weights.
All time values are in minutes unless noted otherwise.
"""

# ── Workflow definition ──────────────────────────────────────────────────────

WORKFLOW_STAGES = [
    "ORDER",
    "PROTOCOL",
    "TECH_CONFIRM",
    "CHECKIN",
    "WORKLIST",
    "PROCEDURE",
    "ORR",
    "REPORT",
    "COMPLETE",
]

# Maps each active stage to the timestamp column that marks its start
STAGE_START_COLUMN = {
    "ORDER":        "order_time",
    "PROTOCOL":     "protocol_start_time",
    "TECH_CONFIRM": "tech_confirm_time",
    "CHECKIN":      "checkin_time",
    "WORKLIST":     "worklist_time",
    "PROCEDURE":    "procedure_start_time",
    "ORR":          "orr_time",
    "REPORT":       "report_start_time",
    "COMPLETE":     "report_complete_time",
}

# Transition labels used in SLA and RCA lookups
STAGE_TRANSITIONS = [
    ("ORDER",        "PROTOCOL",     "ORDER_TO_PROTOCOL"),
    ("PROTOCOL",     "TECH_CONFIRM", "PROTOCOL_TO_TECH_CONFIRM"),
    ("TECH_CONFIRM", "CHECKIN",      "TECH_CONFIRM_TO_CHECKIN"),
    ("CHECKIN",      "WORKLIST",     "CHECKIN_TO_WORKLIST"),
    ("WORKLIST",     "PROCEDURE",    "WORKLIST_TO_PROCEDURE"),
    ("PROCEDURE",    "ORR",          "PROCEDURE_TO_ORR"),
    ("ORR",          "REPORT",       "ORR_TO_REPORT"),
    ("REPORT",       "COMPLETE",     "REPORT_TO_COMPLETE"),
]

# ── Total order-to-report SLA (minutes) ─────────────────────────────────────

TOTAL_SLA_MINUTES = {
    "STAT":    120,   # 2 hours
    "URGENT":  240,   # 4 hours
    "ROUTINE": 1440,  # 24 hours
}

# ── Per-stage maximum expected duration (minutes) ───────────────────────────

STAGE_MAX_MINUTES = {
    "STAT": {
        "ORDER_TO_PROTOCOL":        15,
        "PROTOCOL_TO_TECH_CONFIRM": 10,
        "TECH_CONFIRM_TO_CHECKIN":  20,
        "CHECKIN_TO_WORKLIST":      10,
        "WORKLIST_TO_PROCEDURE":    15,
        "PROCEDURE_TO_ORR":         30,
        "ORR_TO_REPORT":            20,
        "REPORT_TO_COMPLETE":       0,   # report IS the final deliverable
    },
    "URGENT": {
        "ORDER_TO_PROTOCOL":        30,
        "PROTOCOL_TO_TECH_CONFIRM": 20,
        "TECH_CONFIRM_TO_CHECKIN":  30,
        "CHECKIN_TO_WORKLIST":      20,
        "WORKLIST_TO_PROCEDURE":    30,
        "PROCEDURE_TO_ORR":         60,
        "ORR_TO_REPORT":            55,
        "REPORT_TO_COMPLETE":       0,
    },
    "ROUTINE": {
        "ORDER_TO_PROTOCOL":        240,
        "PROTOCOL_TO_TECH_CONFIRM": 120,
        "TECH_CONFIRM_TO_CHECKIN":  180,
        "CHECKIN_TO_WORKLIST":      60,
        "WORKLIST_TO_PROCEDURE":    180,
        "PROCEDURE_TO_ORR":         360,
        "ORR_TO_REPORT":            300,
        "REPORT_TO_COMPLETE":       0,
    },
}

# ── Stuck-case detection: max minutes at a stage with no forward movement ───

STUCK_THRESHOLDS = {
    "STAT": {
        "ORDER":        10,
        "PROTOCOL":      8,
        "TECH_CONFIRM": 15,
        "CHECKIN":       8,
        "WORKLIST":     12,
        "PROCEDURE":    45,
        "ORR":          20,
        "REPORT":       15,
    },
    "URGENT": {
        "ORDER":        20,
        "PROTOCOL":     15,
        "TECH_CONFIRM": 25,
        "CHECKIN":      15,
        "WORKLIST":     25,
        "PROCEDURE":    90,
        "ORR":          45,
        "REPORT":       40,
    },
    "ROUTINE": {
        "ORDER":        120,
        "PROTOCOL":      90,
        "TECH_CONFIRM": 120,
        "CHECKIN":       60,
        "WORKLIST":     120,
        "PROCEDURE":    240,
        "ORR":          180,
        "REPORT":       200,
    },
}

# ── Modality complexity (multiplies expected procedure duration) ─────────────

MODALITY_COMPLEXITY = {
    "MRI":    3.0,
    "CT":     1.5,
    "NM":     2.5,
    "US":     1.2,
    "XR":     0.5,
    "XRAY":   0.5,
    "FLUORO": 1.0,
    "MAMMO":  1.0,
    "DEXA":   0.6,
}

# Base procedure durations by modality (minutes, used when no history exists)
MODALITY_BASE_PROCEDURE_MINUTES = {
    "MRI":    60,
    "CT":     20,
    "NM":     90,
    "US":     30,
    "XR":     10,
    "XRAY":   10,
    "FLUORO": 25,
    "MAMMO":  20,
    "DEXA":   15,
}

# ── Priority scoring weights ─────────────────────────────────────────────────

PRIORITY_BASE_SCORE = {
    "STAT":    1000,
    "URGENT":  500,
    "ROUTINE": 100,
}

# ── Risk score bands ─────────────────────────────────────────────────────────

RISK_BAND = {
    "CRITICAL": 0.85,   # ≥ this → CRITICAL
    "HIGH":     0.65,
    "MEDIUM":   0.45,
    "LOW":      0.25,
    # below LOW → INFO
}

# ── Alert categories ─────────────────────────────────────────────────────────

ALERT_CATEGORIES = {
    "STUCK_CASE":         "Case has not progressed beyond current stage",
    "SLA_BREACH_IMMINENT":"Case is projected to breach SLA",
    "SLA_BREACHED":       "Case has already exceeded SLA",
    "QUEUE_BUILDUP":      "Abnormal queue depth detected at stage",
    "RESOURCE_GAP":       "Insufficient staffing or room availability detected",
}

# ── Reporting ────────────────────────────────────────────────────────────────

REPORT_OUTPUT_DIR = "reports"
DAILY_REPORT_FILENAME = "radiology_daily_report.xlsx"
EXECUTIVE_SUMMARY_FILENAME = "executive_summary.txt"
ALERT_LOG_FILENAME = "alerts.xlsx"
