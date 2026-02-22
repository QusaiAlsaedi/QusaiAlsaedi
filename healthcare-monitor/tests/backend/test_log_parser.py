"""
Unit tests for log-based HL7 parser (Option B detection).
"""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../backend'))

from app.services.log_parser import LogParser, ParsedEvent


@pytest.fixture
def parser():
    return LogParser()  # Uses built-in patterns


class TestRhapsodyOutbound:
    def test_parses_ctrl_id(self, parser):
        line = "2024/02/21 14:05:23 INFO  [OutputCommPoint|TCP-RIS] Sent message MSG_20240221_001234 (ORM^O01)"
        event = parser.parse_line(line)
        assert event is not None
        assert event.event_type == "outbound_message"
        assert event.message_control_id == "MSG_20240221_001234"

    def test_parses_message_type(self, parser):
        line = "2024/02/21 14:05:23 INFO  [OutputCommPoint|TCP-RIS] Sent message MSG001 (ORM^O01)"
        event = parser.parse_line(line)
        assert event.message_type == "ORM"
        assert event.message_event == "O01"

    def test_parses_oru(self, parser):
        line = "2024/02/21 14:10:00 INFO  [OutputCommPoint|TCP-HIS] Sent message RESULT_001 (ORU^R01)"
        event = parser.parse_line(line)
        assert event.message_type == "ORU"
        assert event.message_event == "R01"


class TestRhapsodyAck:
    def test_parses_ack_aa(self, parser):
        line = "2024/02/21 14:05:24 INFO  [InputCommPoint|ACK-RIS] Received ACK (AA) for MSG001"
        event = parser.parse_line(line)
        assert event is not None
        assert event.event_type == "inbound_ack"
        assert event.ack_code == "AA"
        assert event.message_control_id == "MSG001"

    def test_parses_ack_ae(self, parser):
        line = "2024/02/21 14:05:24 INFO  [InputCommPoint|ACK-RIS] Received ACK (AE) for MSG001"
        event = parser.parse_line(line)
        assert event.ack_code == "AE"

    def test_parses_ack_ar(self, parser):
        line = "2024/02/21 14:05:24 INFO  [InputCommPoint|ACK-RIS] Received ACK (AR) for MSG002"
        event = parser.parse_line(line)
        assert event.ack_code == "AR"
        assert event.message_control_id == "MSG002"


class TestRawHL7:
    def test_parses_raw_msh_outbound(self, parser):
        line = "MSH|^~\\&|HIS|HOSPITAL|RIS|RADIOLOGY|20240221140523||ORM^O01|MSG_CTRL_001|P|2.4"
        event = parser.parse_line(line)
        assert event is not None
        assert event.event_type == "outbound_message"
        assert event.message_control_id == "MSG_CTRL_001"
        assert event.message_type == "ORM"

    def test_parses_raw_msa_ack(self, parser):
        line = "MSA|AA|MSG_CTRL_001|Message accepted"
        event = parser.parse_line(line)
        assert event is not None
        assert event.event_type == "inbound_ack"
        assert event.ack_code == "AA"
        assert event.message_control_id == "MSG_CTRL_001"

    def test_no_match_returns_none(self, parser):
        line = "INFO: System startup complete at 14:00:00"
        event = parser.parse_line(line)
        assert event is None

    def test_empty_line_returns_none(self, parser):
        assert parser.parse_line("") is None
        assert parser.parse_line("   ") is None


class TestBatchParsing:
    def test_parse_lines(self, parser):
        lines = [
            "2024/02/21 14:05:23 INFO  [OutputCommPoint|TCP-RIS] Sent message MSG001 (ORM^O01)",
            "INFO: Processing queue",
            "2024/02/21 14:05:24 INFO  [InputCommPoint|ACK-RIS] Received ACK (AA) for MSG001",
        ]
        events = parser.parse_lines(lines)
        assert len(events) == 2
        assert events[0].event_type == "outbound_message"
        assert events[1].event_type == "inbound_ack"
