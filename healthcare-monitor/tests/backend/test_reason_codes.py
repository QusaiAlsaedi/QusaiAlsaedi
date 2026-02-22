"""
Unit tests for reason code correlator.
Tests the _pick_reason function with various evidence combinations.
"""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../backend'))

from app.core.reason_codes import CorrelationContext, _pick_reason, build_bite_text


def make_ctx(**kwargs) -> CorrelationContext:
    ctx = CorrelationContext(
        host="192.168.1.100", port=6661,
        server_name="RIS-APP-01", flow_name="HIS->RIS ORM",
        receiving_app="RADIOLOGY_RIS",
    )
    for k, v in kwargs.items():
        setattr(ctx, k, v)
    return ctx


class TestPickReason:
    def test_port_down_wins_highest_priority(self):
        ctx = make_ctx(port_down=True, consecutive_port_failures=3,
                       cpu_avg_pct=98, disk_max_pct=99)
        code, conf = _pick_reason(ctx)
        assert code == "NETWORK_FAILURE"
        assert conf == "high"

    def test_engine_down_second_priority(self):
        ctx = make_ctx(services_stopped=["rhapsody"], cpu_avg_pct=98)
        code, conf = _pick_reason(ctx)
        assert code == "ENGINE_DOWN"
        assert conf == "high"

    def test_disk_full_third(self):
        ctx = make_ctx(disk_max_pct=97)
        code, conf = _pick_reason(ctx)
        assert code == "DISK_FULL"
        assert conf == "high"

    def test_cpu_overload(self):
        ctx = make_ctx(cpu_avg_pct=92)
        code, conf = _pick_reason(ctx)
        assert code == "CPU_OVERLOAD"
        assert conf == "medium"

    def test_flow_congestion(self):
        ctx = make_ctx(stuck_count=25)
        code, conf = _pick_reason(ctx)
        assert code == "FLOW_CONGESTION"
        assert conf == "medium"

    def test_log_error_pattern(self):
        ctx = make_ctx(log_errors=3)
        code, conf = _pick_reason(ctx)
        assert code == "LOG_ERROR_PATTERN"
        assert conf == "medium"

    def test_unknown_delay_fallback(self):
        ctx = make_ctx()
        code, conf = _pick_reason(ctx)
        assert code == "UNKNOWN_DELAY"
        assert conf == "low"

    def test_port_down_single_failure_medium_conf(self):
        ctx = make_ctx(port_down=True, consecutive_port_failures=1)
        code, conf = _pick_reason(ctx)
        assert code == "NETWORK_FAILURE"
        assert conf == "medium"

    def test_high_cpu_below_90_low_conf(self):
        ctx = make_ctx(cpu_avg_pct=82)
        code, conf = _pick_reason(ctx)
        assert code == "CPU_OVERLOAD"
        assert conf == "low"


class TestBuildBiteText:
    def test_bite_text_ascii_only(self):
        ctx = make_ctx(
            port_down=True, consecutive_port_failures=3,
            cpu_avg_pct=94, disk_max_pct=67, reason_code="NETWORK_FAILURE",
            confidence="high",
        )
        bite = build_bite_text(
            ctx=ctx, severity="critical", message_type="ORM",
            stuck_messages=[{"message_control_id": f"MSG{i:03d}"} for i in range(50)],
            oldest_age_seconds=1200,
        )
        # ASCII only - no non-ASCII characters
        assert all(ord(c) < 128 for c in bite), "BITE text contains non-ASCII characters"

    def test_bite_text_contains_key_fields(self):
        ctx = make_ctx(reason_code="NETWORK_FAILURE", confidence="high")
        bite = build_bite_text(
            ctx=ctx, severity="critical", message_type="ORM",
            stuck_messages=[{"message_control_id": "MSG001"}],
            oldest_age_seconds=120,
        )
        assert "NETWORK_FAILURE" in bite
        assert "ORM" in bite
        assert "192.168.1.100" in bite
        assert "6661" in bite
        assert "CRITICAL" in bite.upper()

    def test_bite_text_impact_line(self):
        ctx = make_ctx(reason_code="UNKNOWN_DELAY", confidence="low", stuck_count=10)
        bite = build_bite_text(
            ctx=ctx, severity="warning", message_type="ORU",
            stuck_messages=[{"message_control_id": f"R{i}"} for i in range(10)],
            oldest_age_seconds=65,
        )
        assert "1m" in bite   # oldest age formatted
        assert "10" in bite   # stuck count

    def test_more_than_3_ctrl_ids_truncated(self):
        ctx = make_ctx(reason_code="UNKNOWN_DELAY", confidence="low")
        msgs = [{"message_control_id": f"MSG{i:04d}"} for i in range(10)]
        bite = build_bite_text(
            ctx=ctx, severity="info", message_type="ORM",
            stuck_messages=msgs, oldest_age_seconds=45,
        )
        assert "+7 more" in bite or "more" in bite
