"""
Unit tests for PHI masking module.
These are the most compliance-critical tests in the codebase.
"""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../backend'))

from app.core.phi_masking import (
    mask_hl7_segment, mask_log_line, extract_patient_id_masked, _mask_mrn
)


class TestMaskMrn:
    def test_long_mrn_keeps_last_4(self):
        result = _mask_mrn("1234567890")
        assert result == "MRN-***7890"
        assert "1234" not in result
        assert "567" not in result

    def test_short_mrn(self):
        result = _mask_mrn("123")
        assert "***" in result
        assert "123" not in result

    def test_mrn_prefix_present(self):
        assert _mask_mrn("9998887777").startswith("MRN-")


class TestMaskPIDSegment:
    def test_patient_name_blanked(self):
        pid = "PID|1||123456789^^^HIS^MRN|||||DOE^JOHN|||||||||||"
        masked = mask_hl7_segment(pid)
        assert "DOE" not in masked
        assert "JOHN" not in masked

    def test_mrn_masked_not_removed(self):
        pid = "PID|1||123456789^^^HIS^MRN|||||||||||||||"
        masked = mask_hl7_segment(pid)
        # Should contain a masked form, not the raw MRN
        assert "123456789" not in masked
        assert "MRN-***" in masked or "***" in masked

    def test_mrn_last_4_retained(self):
        pid = "PID|1||1234567890^^^HIS^MRN|||||||||||||||"
        masked = mask_hl7_segment(pid)
        assert "7890" in masked   # last 4 digits preserved for traceability

    def test_dob_blanked(self):
        pid = "PID|1||MRN123|||19850101|||||||||||||||"
        masked = mask_hl7_segment(pid)
        fields = masked.split("|")
        # PID-7 (index 7) should be blanked
        assert fields[7] in ("***", "****-**-**", "")

    def test_ssn_field_blanked(self):
        pid = "PID|1||MRN123|||||||||||123-45-6789||||"
        masked = mask_hl7_segment(pid)
        assert "123-45-6789" not in masked

    def test_msh_segment_unchanged(self):
        msh = "MSH|^~\\&|HIS|HOSPITAL|RIS|RADIOLOGY|20240221140523||ORM^O01|MSG001|P|2.4"
        result = mask_hl7_segment(msh)
        assert result == msh  # MSH is safe, no PHI

    def test_non_pid_non_msh_returns_segment(self):
        obr = "OBR|1|ORD001|ACC001|71046^CHEST XRAY|||20240221|||||||||||"
        result = mask_hl7_segment(obr)
        assert result == obr  # OBR returned as-is (no PHI masking defined)


class TestMaskLogLine:
    def test_ssn_masked(self):
        line = "Patient SSN: 123-45-6789 admitted"
        masked = mask_log_line(line)
        assert "123-45-6789" not in masked
        assert "XXXX" in masked

    def test_phone_masked(self):
        line = "Contact: 555-867-5309"
        masked = mask_log_line(line)
        assert "555-867-5309" not in masked

    def test_mrn_pattern_masked(self):
        line = "Processing MRN: 9998887777 for order"
        masked = mask_log_line(line)
        assert "9998887777" not in masked or "MRN-***" in masked

    def test_pid_segment_in_log_masked(self):
        line = "Received: PID|1||123456789^^^HIS^MRN||DOE^JANE|||||||||||"
        masked = mask_log_line(line)
        assert "DOE" not in masked
        assert "JANE" not in masked

    def test_clean_line_unchanged(self):
        line = "INFO: Message MSG001 sent to 192.168.1.100:6661 at 14:05:23"
        masked = mask_log_line(line)
        # No PHI, should be effectively unchanged (minus possible SSN-pattern false positives)
        assert "MSG001" in masked
        assert "192.168.1.100" in masked


class TestExtractPatientIdMasked:
    def test_extracts_and_masks(self):
        pid = "PID|1||1234567890^^^HIS^MRN|||||||||||||||"
        result = extract_patient_id_masked(pid)
        assert result is not None
        assert "7890" in result
        assert "1234" not in result

    def test_empty_pid(self):
        assert extract_patient_id_masked("") is None

    def test_missing_pid3(self):
        pid = "PID|1||||"
        result = extract_patient_id_masked(pid)
        assert result is None or result == "***"
