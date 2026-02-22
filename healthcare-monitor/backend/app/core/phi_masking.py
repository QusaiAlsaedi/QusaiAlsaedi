"""
PHI masking utilities.

HIPAA Safe Harbor: mask all 18 PHI identifiers before storage or logging.
This module is the ONLY place raw HL7 segments should be processed.
Never log or store unmasked PHI outside of this layer.

Masking strategy:
  - MRN / Patient ID  -> "MRN-***" + last 4 digits
  - Patient name      -> "***,***"
  - Date of birth     -> "****-**-**"
  - Phone number      -> "***-***-XXXX" (keep last 4 for correlation tracing)
  - Address           -> "***"
  - SSN               -> "***-**-XXXX"
  - Free text         -> redact with [PHI-REDACTED]
"""
import re
from typing import Optional


# HL7 segment field separator
_HL7_FIELD_SEP = "|"

# Regex patterns for common PHI in free text / logs
_MRN_PATTERNS = [
    re.compile(r"(?i)(MRN|patient[\s_-]?id|pid[\s-]?3)\s*[=:]\s*(\d{4,20})", re.I),
    re.compile(r"\bMRN(\d{4,20})\b"),
]
_SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE_PATTERN = re.compile(r"\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
_DOB_PATTERN = re.compile(r"\b\d{4}[-/]\d{2}[-/]\d{2}\b|\b\d{8}\b")


def _mask_mrn(mrn: str) -> str:
    """Keep last 4 digits for traceability, mask the rest."""
    if len(mrn) <= 4:
        return "MRN-" + "*" * len(mrn)
    return "MRN-***" + mrn[-4:]


def mask_hl7_segment(segment: str) -> str:
    """
    Mask PHI in a single HL7 segment string (MSH, PID, PV1, etc.).
    Only the segment header (e.g. MSH line) is safe to store unmasked.
    PID segments must always pass through here before storage.
    """
    if not segment:
        return segment

    seg_type = segment[:3].upper()

    if seg_type == "PID":
        return _mask_pid_segment(segment)
    elif seg_type == "MSH":
        # MSH itself contains no PHI - safe to store as-is
        return segment
    elif seg_type in ("PV1", "PV2"):
        # Strip attending physician name fields (PV1-7, PV1-8, PV1-9)
        return _mask_segment_fields(segment, fields_to_blank=[7, 8, 9])
    elif seg_type == "ORC":
        # ORC-12 = ordering provider
        return _mask_segment_fields(segment, fields_to_blank=[12])
    elif seg_type == "OBR":
        # OBR-16 = ordering provider
        return _mask_segment_fields(segment, fields_to_blank=[16])
    else:
        return segment


def _mask_pid_segment(pid: str) -> str:
    """
    Mask all PHI fields in a PID segment.
    PID-3  = Patient ID (MRN list)
    PID-5  = Patient name
    PID-7  = Date of birth
    PID-8  = Administrative sex (keep - not PHI)
    PID-11 = Patient address
    PID-13 = Phone
    PID-14 = Work phone
    PID-18 = Patient account number
    PID-19 = SSN
    """
    fields = pid.split(_HL7_FIELD_SEP)
    masked_fields = list(fields)

    def _blank(idx: int, replacement: str = "***") -> None:
        if idx < len(masked_fields):
            masked_fields[idx] = replacement

    # PID-0 = "PID" literal, PID-1 = set ID
    # Field indices are 1-based in HL7, 0-based in our split list
    if len(masked_fields) > 3:
        # PID-3: patient identifier list - mask MRN portion
        pid3 = masked_fields[3]
        if pid3:
            # CX format: ID^^^Assigning Authority. Keep component 4 (assigning authority)
            components = pid3.split("^")
            if components:
                raw_id = components[0]
                masked_id = _mask_mrn(raw_id) if raw_id.isdigit() else "***"
                components[0] = masked_id
                masked_fields[3] = "^".join(components)

    _blank(5)   # PID-5 patient name
    _blank(6)   # PID-6 mother's maiden name
    _blank(7, "****-**-**")  # PID-7 DOB
    # PID-8 sex - keep
    _blank(11)  # PID-11 address
    _blank(13)  # PID-13 phone home
    _blank(14)  # PID-14 phone work
    _blank(18)  # PID-18 account number
    _blank(19)  # PID-19 SSN

    return _HL7_FIELD_SEP.join(masked_fields)


def _mask_segment_fields(segment: str, fields_to_blank: list[int]) -> str:
    fields = segment.split(_HL7_FIELD_SEP)
    for idx in fields_to_blank:
        if idx < len(fields):
            fields[idx] = "***"
    return _HL7_FIELD_SEP.join(fields)


def extract_patient_id_masked(pid_segment: str) -> Optional[str]:
    """Extract a masked MRN from PID-3 for display."""
    if not pid_segment:
        return None
    fields = pid_segment.split(_HL7_FIELD_SEP)
    if len(fields) > 3:
        pid3 = fields[3]
        if pid3:
            raw_id = pid3.split("^")[0]
            if raw_id:
                return _mask_mrn(raw_id) if raw_id.isdigit() else "***"
    return None


def mask_log_line(line: str) -> str:
    """
    Apply PHI masking to a raw log line before storage.
    Handles common patterns found in interface engine logs.
    """
    # Mask SSN patterns
    line = _SSN_PATTERN.sub("***-**-XXXX", line)
    # Mask phone numbers
    line = _PHONE_PATTERN.sub("***-***-XXXX", line)
    # Mask explicit MRN patterns
    for pat in _MRN_PATTERNS:
        line = pat.sub(lambda m: m.group(1) + "=MRN-***" + (m.group(2)[-4:] if len(m.group(2)) >= 4 else "****"), line)
    # Mask inline PID segments
    if "PID|" in line:
        start = line.index("PID|")
        end = line.find("\r", start)
        if end == -1:
            end = len(line)
        pid_seg = line[start:end]
        line = line[:start] + _mask_pid_segment(pid_seg) + line[end:]
    return line


def mask_free_text(text: str) -> str:
    """Redact free text that may contain PHI (e.g. OBX observation values)."""
    return "[PHI-REDACTED]"


def safe_msh_for_storage(msh: str) -> str:
    """
    MSH segments contain routing info but no direct patient PHI.
    Return as-is after a basic sanity check.
    """
    if not msh.startswith("MSH"):
        return "***"
    return msh
