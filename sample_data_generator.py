"""
Generates a realistic synthetic radiology workbook for demo/testing.

Includes:
  - ~80 active cases across all modalities and priorities
  - Cases at every workflow stage (including several stuck cases)
  - ~200 completed historical cases for baseline building
  - Intentional delay patterns for RCA to surface:
      • MRI exams ~20% slower than average
      • Monday morning queue buildup
      • One technologist (Tech-02) consistently slow
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from radiology_command_center.data_loader import write_excel_report

random.seed(42)

# ── Reference parameters ─────────────────────────────────────────────────────

MODALITIES = ["CT", "MRI", "US", "XR", "NM", "FLUORO", "MAMMO"]
MODALITY_WEIGHTS = [30, 20, 20, 15, 5, 5, 5]   # probability weight

PRIORITIES = ["STAT", "URGENT", "ROUTINE"]
PRIORITY_WEIGHTS = [10, 25, 65]

WORKFLOW_TYPES = ["scheduled", "walk-in"]
WORKFLOW_WEIGHTS = [70, 30]

PHYSICIANS = [
    "Dr. Ahmed Al-Rashid", "Dr. Fatima Hassan", "Dr. Omar Khalid",
    "Dr. Sarah Mitchell", "Dr. James Patel", "Dr. Layla Nasser",
]

RADIOLOGISTS = [
    "Dr. Chen Wei", "Dr. Maria Garcia", "Dr. Yusuf Al-Amin", "Dr. Anna Kowalski"
]

TECHNOLOGISTS = ["Tech-01", "Tech-02", "Tech-03", "Tech-04", "Tech-05"]

ROOMS = {
    "CT":     ["CT-1", "CT-2"],
    "MRI":    ["MRI-1", "MRI-2"],
    "US":     ["US-1", "US-2", "US-3"],
    "XR":     ["XR-1", "XR-2", "XR-3"],
    "NM":     ["NM-1"],
    "FLUORO": ["FL-1"],
    "MAMMO":  ["MA-1"],
}

PROCEDURES = {
    "CT":     ["CT Chest w/o Contrast", "CT Abdomen/Pelvis w Contrast",
               "CT Head w/o Contrast", "CT Angiography Chest"],
    "MRI":    ["MRI Brain w/o Contrast", "MRI Lumbar Spine",
               "MRI Knee", "MRI Abdomen w/wo Contrast"],
    "US":     ["US Abdomen Complete", "US Pelvis Transabdominal",
               "US Thyroid", "US Renal"],
    "XR":     ["XR Chest PA/Lateral", "XR Abdomen",
               "XR Knee AP/Lateral", "XR Lumbar Spine"],
    "NM":     ["NM Bone Scan", "NM Thyroid Scan", "NM Lung V/Q"],
    "FLUORO": ["Fluoro Upper GI", "Fluoro MBSS"],
    "MAMMO":  ["Mammogram Bilateral Screening", "Mammogram Diagnostic"],
}

FIRST_NAMES = [
    "Ahmed", "Fatima", "Omar", "Sara", "Ali", "Nora", "Khalid", "Layla",
    "Hassan", "Aisha", "Yousef", "Hind", "Tariq", "Reem", "Faisal", "Dana",
    "Mohammed", "Salma", "Ibrahim", "Maha", "Abdullah", "Rana", "Samir", "Lina",
]
LAST_NAMES = [
    "Al-Rashid", "Hassan", "Al-Farsi", "Malik", "Al-Otaibi", "Khalid",
    "Nasser", "Al-Sayed", "Ibrahim", "Al-Amin", "Saleh", "Al-Hamdan",
]


def _rnd_name() -> str:
    return f"{random.choice(LAST_NAMES)}, {random.choice(FIRST_NAMES)}"


def _rnd_mrn() -> str:
    return f"MRN{random.randint(100000, 999999)}"


def _rnd_case_id(n: int) -> str:
    return f"ACC-2024{random.randint(1,12):02d}{random.randint(1,28):02d}-{n:04d}"


def _stage_duration(
    modality: str,
    priority: str,
    stage_label: str,
    technologist: str,
    is_monday_morning: bool,
) -> float:
    """Return a realistic duration with injected delay patterns."""
    base_map = {
        "ORDER_TO_PROTOCOL":        {"STAT": 12, "URGENT": 25, "ROUTINE": 90},
        "PROTOCOL_TO_TECH_CONFIRM": {"STAT":  8, "URGENT": 18, "ROUTINE": 60},
        "TECH_CONFIRM_TO_CHECKIN":  {"STAT": 15, "URGENT": 28, "ROUTINE": 120},
        "CHECKIN_TO_WORKLIST":      {"STAT":  8, "URGENT": 18, "ROUTINE": 55},
        "WORKLIST_TO_PROCEDURE":    {"STAT": 12, "URGENT": 25, "ROUTINE": 150},
        "PROCEDURE_TO_ORR":         {"STAT": 25, "URGENT": 55, "ROUTINE": 300},
        "ORR_TO_REPORT":            {"STAT": 18, "URGENT": 50, "ROUTINE": 250},
        "REPORT_TO_COMPLETE":       {"STAT":  0, "URGENT":  0, "ROUTINE":   0},
    }
    base = base_map.get(stage_label, {}).get(priority, 60)
    jitter = random.gauss(0, base * 0.20)
    duration = max(1.0, base + jitter)

    # Injected patterns for RCA demo
    if modality == "MRI":
        duration *= 1.20  # MRI 20% slower

    if technologist == "Tech-02":
        duration *= 1.30  # Slow tech pattern

    if is_monday_morning:
        duration *= 1.25  # Monday AM queue

    return round(duration, 1)


def _build_timestamps(
    order_time: datetime,
    modality: str,
    priority: str,
    technologist: str,
    stages_to_complete: int,  # 0 = just ordered, 7 = fully complete
) -> Dict[str, Optional[datetime]]:
    """
    Build a consistent chain of timestamps up to stages_to_complete.
    stages_to_complete:
      0 → ORDER only
      1 → + PROTOCOL
      2 → + TECH_CONFIRM
      3 → + CHECKIN
      4 → + WORKLIST
      5 → + PROCEDURE
      6 → + ORR
      7 → + REPORT (complete)
    """
    labels = [
        "ORDER_TO_PROTOCOL",
        "PROTOCOL_TO_TECH_CONFIRM",
        "TECH_CONFIRM_TO_CHECKIN",
        "CHECKIN_TO_WORKLIST",
        "WORKLIST_TO_PROCEDURE",
        "PROCEDURE_TO_ORR",
        "ORR_TO_REPORT",
    ]
    ts_names = [
        ("order_time",              "protocol_start_time"),
        ("protocol_start_time",     "tech_confirm_time"),
        ("tech_confirm_time",       "checkin_time"),
        ("checkin_time",            "worklist_time"),
        ("worklist_time",           "procedure_start_time"),
        ("procedure_start_time",    "orr_time"),
        ("orr_time",                "report_complete_time"),
    ]
    extra = [
        # protocol_complete_time is between protocol_start and tech_confirm
        # procedure_complete_time is between procedure_start and orr
        # report_start_time is between orr and report_complete
    ]

    is_monday_morning = (
        order_time.weekday() == 0 and order_time.hour < 12
    )

    ts: Dict[str, Optional[datetime]] = {
        "order_time": order_time,
        "protocol_start_time": None,
        "protocol_complete_time": None,
        "tech_confirm_time": None,
        "checkin_time": None,
        "worklist_time": None,
        "procedure_start_time": None,
        "procedure_complete_time": None,
        "orr_time": None,
        "report_start_time": None,
        "report_complete_time": None,
    }

    current_time = order_time
    for i in range(min(stages_to_complete, len(labels))):
        label = labels[i]
        duration = _stage_duration(modality, priority, label, technologist, is_monday_morning)
        next_time = current_time + timedelta(minutes=duration)
        ts[ts_names[i][1]] = next_time

        # Fill in extra mid-stage timestamps
        if label == "ORDER_TO_PROTOCOL":
            # protocol_complete ≈ 80% through this stage
            ts["protocol_complete_time"] = current_time + timedelta(minutes=duration * 0.8)
        elif label == "WORKLIST_TO_PROCEDURE":
            # procedure_complete ≈ after scanning (add procedure duration)
            proc_dur = {"CT": 20, "MRI": 55, "US": 28, "XR": 8, "NM": 85, "FLUORO": 25, "MAMMO": 20}
            scan_time = proc_dur.get(modality, 20)
            ts["procedure_complete_time"] = next_time + timedelta(minutes=scan_time * random.uniform(0.8, 1.2))
        elif label == "ORR_TO_REPORT":
            ts["report_start_time"] = current_time + timedelta(minutes=duration * 0.1)

        current_time = next_time

    return ts


def _make_row(case_id: str, n: int, base_date: datetime, stages: int) -> Dict:
    modality  = random.choices(MODALITIES, MODALITY_WEIGHTS)[0]
    priority  = random.choices(PRIORITIES, PRIORITY_WEIGHTS)[0]
    wf_type   = random.choices(WORKFLOW_TYPES, WORKFLOW_WEIGHTS)[0]
    tech      = random.choice(TECHNOLOGISTS)
    rad       = random.choice(RADIOLOGISTS)
    physician = random.choice(PHYSICIANS)

    # Spread orders across the day
    hour_offset = random.uniform(0, 9 * 60)  # 0–9 hours
    order_time = base_date + timedelta(minutes=hour_offset)

    ts = _build_timestamps(order_time, modality, priority, tech, stages)

    row = {
        "case_id":            case_id,
        "mrn":                _rnd_mrn(),
        "patient_name":       _rnd_name(),
        "age":                random.randint(18, 85),
        "gender":             random.choice(["M", "F"]),
        "modality":           modality,
        "procedure":          random.choice(PROCEDURES[modality]),
        "priority":           priority,
        "workflow_type":      wf_type,
        "ordering_physician": physician,
        "reading_radiologist":rad,
        "technologist":       tech,
        "room":               random.choice(ROOMS[modality]),
        "notes":              "",
    }
    row.update(ts)
    return row


def generate_active_cases(base_date: datetime, n: int = 80) -> List[Dict]:
    """Generate n active cases spread across all workflow stages."""
    rows = []
    stage_dist = [0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6]  # more cases mid-pipeline
    for i in range(n):
        stages = random.choice(stage_dist)
        # Inject some stuck cases
        if i % 15 == 0 and stages > 0:
            stages = max(0, stages - 1)  # keep at earlier stage longer

        row = _make_row(_rnd_case_id(i + 1), i, base_date, stages)

        # For STAT cases that haven't completed, push order time closer to now
        if row["priority"] == "STAT" and stages < 7:
            recent_offset = random.uniform(10, 90)
            row["order_time"] = base_date + timedelta(minutes=recent_offset)
            row = _make_row(row["case_id"], i, row["order_time"], stages)

        rows.append(row)
    return rows


def generate_historical_cases(base_date: datetime, n: int = 200) -> List[Dict]:
    """Generate n completed cases spread over the past 30 days."""
    rows = []
    for i in range(n):
        days_ago = random.randint(0, 30)
        hour = random.randint(7, 19)
        order_time = base_date - timedelta(days=days_ago, hours=random.randint(0, 8)) + timedelta(hours=hour - 8)
        row = _make_row(_rnd_case_id(1000 + i), i, order_time, stages=7)
        rows.append(row)
    return rows


def _rows_to_df(rows: List[Dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    # Format timestamps for Excel
    ts_cols = [
        "order_time", "protocol_start_time", "protocol_complete_time",
        "tech_confirm_time", "checkin_time", "worklist_time",
        "procedure_start_time", "procedure_complete_time",
        "orr_time", "report_start_time", "report_complete_time",
    ]
    for col in ts_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col])
    return df


def generate_sample_workbook(
    output_path: str,
    base_date: Optional[datetime] = None,
) -> None:
    """Write a sample workbook with Cases and Historical sheets."""
    if base_date is None:
        base_date = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)

    print(f"  Generating active cases (base date: {base_date.strftime('%Y-%m-%d %H:%M')}) …")
    active_rows = generate_active_cases(base_date, n=80)
    active_df = _rows_to_df(active_rows)

    print("  Generating historical cases (last 30 days) …")
    hist_rows = generate_historical_cases(base_date, n=200)
    hist_df = _rows_to_df(hist_rows)

    write_excel_report(output_path, {"Cases": active_df, "Historical": hist_df})
    print(f"  Sample workbook written: {output_path}")
    print(f"    Active cases  : {len(active_df)}")
    print(f"    Historical    : {len(hist_df)}")


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "sample_data.xlsx"
    generate_sample_workbook(out)
