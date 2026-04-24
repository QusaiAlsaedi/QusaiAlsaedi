"""
Excel ↔ RadiologyCase bridge.

Expected workbook layout
------------------------
Sheet "Cases"     – active / in-progress cases (one row per case)
Sheet "Historical"– completed cases used to build stage-duration baselines

Column headers are case-insensitive and spaces are normalised to underscores.
Missing optional columns are silently ignored.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from radiology_command_center.models import RadiologyCase

logger = logging.getLogger(__name__)

# Map of flexible header aliases → canonical field name
_COLUMN_ALIASES: Dict[str, str] = {
    "case_id":              "case_id",
    "accession":            "case_id",
    "accession_number":     "case_id",
    "mrn":                  "mrn",
    "patient_id":           "mrn",
    "patient_name":         "patient_name",
    "name":                 "patient_name",
    "age":                  "age",
    "gender":               "gender",
    "sex":                  "gender",
    "modality":             "modality",
    "procedure":            "procedure",
    "procedure_name":       "procedure",
    "exam":                 "procedure",
    "priority":             "priority",
    "order_priority":       "priority",
    "workflow_type":        "workflow_type",
    "type":                 "workflow_type",
    "ordering_physician":   "ordering_physician",
    "referring_physician":  "ordering_physician",
    "physician":            "ordering_physician",
    "reading_radiologist":  "reading_radiologist",
    "radiologist":          "reading_radiologist",
    "rad":                  "reading_radiologist",
    "technologist":         "technologist",
    "tech":                 "technologist",
    "room":                 "room",
    "scanner_room":         "room",
    "notes":                "notes",
    "comments":             "notes",
    # Timestamps
    "order_time":               "order_time",
    "order_datetime":           "order_time",
    "order_date_time":          "order_time",
    "protocol_start_time":      "protocol_start_time",
    "protocol_start":           "protocol_start_time",
    "protocol_complete_time":   "protocol_complete_time",
    "protocol_complete":        "protocol_complete_time",
    "protocol_end_time":        "protocol_complete_time",
    "tech_confirm_time":        "tech_confirm_time",
    "tech_confirmation":        "tech_confirm_time",
    "tech_confirm":             "tech_confirm_time",
    "checkin_time":             "checkin_time",
    "check_in_time":            "checkin_time",
    "check_in":                 "checkin_time",
    "worklist_time":            "worklist_time",
    "worklist":                 "worklist_time",
    "wl_time":                  "worklist_time",
    "procedure_start_time":     "procedure_start_time",
    "procedure_start":          "procedure_start_time",
    "exam_start":               "procedure_start_time",
    "procedure_complete_time":  "procedure_complete_time",
    "procedure_complete":       "procedure_complete_time",
    "exam_complete":            "procedure_complete_time",
    "exam_end":                 "procedure_complete_time",
    "orr_time":                 "orr_time",
    "orr":                      "orr_time",
    "order_result_review":      "orr_time",
    "report_start_time":        "report_start_time",
    "report_start":             "report_start_time",
    "reporting_start":          "report_start_time",
    "report_complete_time":     "report_complete_time",
    "report_complete":          "report_complete_time",
    "report_signed":            "report_complete_time",
    "oru_time":                 "report_complete_time",
    "oru":                      "report_complete_time",
}

_TIMESTAMP_FIELDS = {
    "order_time", "protocol_start_time", "protocol_complete_time",
    "tech_confirm_time", "checkin_time", "worklist_time",
    "procedure_start_time", "procedure_complete_time",
    "orr_time", "report_start_time", "report_complete_time",
}

_INT_FIELDS = {"age"}
_REQUIRED_FIELDS = {"case_id", "mrn", "modality", "priority", "order_time"}


def _normalise_header(h: str) -> str:
    return str(h).strip().lower().replace(" ", "_").replace("-", "_")


def _resolve_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename DataFrame columns using alias map."""
    rename_map = {}
    for col in df.columns:
        canonical = _COLUMN_ALIASES.get(_normalise_header(col))
        if canonical:
            rename_map[col] = canonical
    return df.rename(columns=rename_map)


def _parse_timestamp(value) -> Optional[datetime]:
    if pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    try:
        return pd.to_datetime(str(value)).to_pydatetime()
    except Exception:
        return None


def _row_to_case(row: pd.Series) -> Optional[RadiologyCase]:
    def get(field, default=None):
        v = row.get(field, default)
        if pd.isna(v) if not isinstance(v, (datetime, pd.Timestamp)) else False:
            return default
        return v

    case_id = str(get("case_id", "")).strip()
    if not case_id:
        return None

    kwargs: Dict = {
        "case_id":            case_id,
        "mrn":                str(get("mrn", "")).strip(),
        "patient_name":       str(get("patient_name", "Unknown")).strip(),
        "age":                int(get("age", 0) or 0),
        "gender":             str(get("gender", "U")).strip().upper()[:1],
        "modality":           str(get("modality", "XR")).strip().upper(),
        "procedure":          str(get("procedure", "")).strip(),
        "priority":           str(get("priority", "ROUTINE")).strip().upper(),
        "workflow_type":      str(get("workflow_type", "scheduled")).strip().lower(),
        "ordering_physician": str(get("ordering_physician", "")).strip(),
        "reading_radiologist":_clean_str(get("reading_radiologist")),
        "technologist":       _clean_str(get("technologist")),
        "room":               _clean_str(get("room")),
        "notes":              _clean_str(get("notes")),
    }

    for ts_field in _TIMESTAMP_FIELDS:
        kwargs[ts_field] = _parse_timestamp(get(ts_field))

    try:
        return RadiologyCase(**kwargs)
    except TypeError as exc:
        logger.warning("Skipping case %s: %s", case_id, exc)
        return None


def _clean_str(v) -> Optional[str]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s if s else None


def load_cases_from_excel(
    filepath: str,
    sheet_name: str = "Cases",
) -> Tuple[List[RadiologyCase], List[str]]:
    """
    Load active cases from an Excel workbook.
    Returns (cases, warnings).
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Input file not found: {filepath}")

    warnings: List[str] = []

    try:
        df = pd.read_excel(filepath, sheet_name=sheet_name, dtype=str)
    except Exception as exc:
        raise ValueError(f"Cannot read sheet '{sheet_name}' from {filepath}: {exc}") from exc

    if df.empty:
        return [], [f"Sheet '{sheet_name}' is empty."]

    df = _resolve_columns(df)

    missing_required = _REQUIRED_FIELDS - set(df.columns)
    if missing_required:
        warnings.append(f"Missing required columns: {missing_required}. Results may be incomplete.")

    cases: List[RadiologyCase] = []
    for idx, row in df.iterrows():
        case = _row_to_case(row)
        if case:
            cases.append(case)
        else:
            warnings.append(f"Row {idx + 2}: skipped (missing case_id or parse error)")

    logger.info("Loaded %d cases from '%s'", len(cases), filepath)
    return cases, warnings


def load_historical_from_excel(
    filepath: str,
    sheet_name: str = "Historical",
) -> Tuple[List[RadiologyCase], List[str]]:
    """Load completed historical cases for baseline calculation."""
    try:
        return load_cases_from_excel(filepath, sheet_name=sheet_name)
    except Exception as exc:
        logger.warning("Could not load historical data: %s", exc)
        return [], [str(exc)]


# ── Excel output helpers ─────────────────────────────────────────────────────

def cases_to_dataframe(cases: List[RadiologyCase]) -> pd.DataFrame:
    rows = []
    for c in cases:
        rows.append({
            "Case ID":            c.case_id,
            "MRN":                c.mrn,
            "Patient Name":       c.patient_name,
            "Age":                c.age,
            "Gender":             c.gender,
            "Modality":           c.modality,
            "Procedure":          c.procedure,
            "Priority":           c.priority,
            "Workflow Type":      c.workflow_type,
            "Ordering Physician": c.ordering_physician,
            "Radiologist":        c.reading_radiologist,
            "Technologist":       c.technologist,
            "Room":               c.room,
            "Current Stage":      c.current_stage,
            "Time in Stage (min)":round(c.time_in_stage_minutes, 1),
            "Total Elapsed (min)":round(c.total_elapsed_minutes, 1),
            "SLA Remaining (min)":round(c.sla_remaining_minutes, 1),
            "Breach Risk":        round(c.breach_risk_score, 3),
            "Risk Band":          c.risk_band,
            "Priority Score":     round(c.priority_score, 1),
            "Is Stuck":           c.is_stuck,
            "Stuck Reason":       c.stuck_reason,
            "Order Time":         c.order_time,
            "Report Complete":    c.report_complete_time,
        })
    return pd.DataFrame(rows)


def write_excel_report(
    filepath: str,
    sheets: Dict[str, pd.DataFrame],
    *,
    freeze_header: bool = True,
) -> None:
    """Write multiple named sheets to a single xlsx workbook."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with pd.ExcelWriter(filepath, engine="openpyxl") as writer:
        for sheet, df in sheets.items():
            df.to_excel(writer, sheet_name=sheet[:31], index=False)
            if freeze_header:
                ws = writer.sheets[sheet[:31]]
                ws.freeze_panes = "A2"
    logger.info("Report written: %s", filepath)


def generate_excel_template(filepath: str) -> None:
    """Create a blank input template workbook with correct headers."""
    cases_cols = [
        "case_id", "mrn", "patient_name", "age", "gender",
        "modality", "procedure", "priority", "workflow_type",
        "ordering_physician", "reading_radiologist", "technologist", "room", "notes",
        "order_time", "protocol_start_time", "protocol_complete_time",
        "tech_confirm_time", "checkin_time", "worklist_time",
        "procedure_start_time", "procedure_complete_time",
        "orr_time", "report_start_time", "report_complete_time",
    ]
    hist_cols = cases_cols  # same structure, just completed cases

    example = {
        "case_id": "ACC-20240101-001",
        "mrn": "MRN123456",
        "patient_name": "Doe, John",
        "age": 45,
        "gender": "M",
        "modality": "CT",
        "procedure": "CT Chest w/o Contrast",
        "priority": "URGENT",
        "workflow_type": "scheduled",
        "ordering_physician": "Dr. Smith",
        "reading_radiologist": "Dr. Jones",
        "technologist": "Tech01",
        "room": "CT-1",
        "notes": "",
        "order_time": "2024-01-01 08:00:00",
        "protocol_start_time": "2024-01-01 08:15:00",
        "protocol_complete_time": "2024-01-01 08:20:00",
        "tech_confirm_time": "2024-01-01 08:30:00",
        "checkin_time": "2024-01-01 09:00:00",
        "worklist_time": "2024-01-01 09:05:00",
        "procedure_start_time": "2024-01-01 09:30:00",
        "procedure_complete_time": "2024-01-01 09:50:00",
        "orr_time": "2024-01-01 10:00:00",
        "report_start_time": "2024-01-01 10:10:00",
        "report_complete_time": "2024-01-01 10:40:00",
    }

    cases_df = pd.DataFrame(columns=cases_cols)
    hist_df = pd.DataFrame([example])

    write_excel_report(filepath, {"Cases": cases_df, "Historical": hist_df})
    logger.info("Template written: %s", filepath)
