"""
Unit tests for deterministic timing and latency budget enforcement.
"""

import time
import pytest

from ..core.timing import (
    FrameTimestamp, STAGE_BUDGETS_MS, TOTAL_BUDGET_MS,
    check_budget, elapsed_ms, now_ns,
)


class TestMonotonicClock:

    def test_now_ns_increases(self):
        t1 = now_ns()
        time.sleep(0.001)
        t2 = now_ns()
        assert t2 > t1

    def test_elapsed_ms_positive(self):
        start = now_ns()
        time.sleep(0.01)
        ms = elapsed_ms(start)
        assert ms >= 8.0  # at least 8 ms (giving slack for OS scheduling)
        assert ms < 100.0


class TestFrameTimestamp:

    def test_total_pipeline_ms_sums_stages(self):
        ts = FrameTimestamp(frame_id=1, capture_ns=now_ns())
        ts.preprocess_ms = 2.0
        ts.inference_ms  = 4.5
        ts.postproc_ms   = 1.0
        ts.tracking_ms   = 1.5
        ts.smoothing_ms  = 0.3
        ts.privacy_ms    = 1.0
        ts.publish_ms    = 0.5
        expected = 2.0 + 4.5 + 1.0 + 1.5 + 0.3 + 1.0 + 0.5
        assert abs(ts.total_pipeline_ms - expected) < 0.001

    def test_capture_to_publish_ms(self):
        t0 = now_ns()
        ts = FrameTimestamp(frame_id=1, capture_ns=t0)
        ts.publish_ns = t0 + 15_000_000   # 15 ms later
        assert abs(ts.capture_to_publish_ms - 15.0) < 0.1

    def test_to_dict_has_required_keys(self):
        ts = FrameTimestamp(frame_id=5, capture_ns=now_ns())
        d = ts.to_dict()
        assert "frame_id" in d
        assert "capture_to_publish_ms" in d
        assert "stages_ms" in d
        assert "total" in d["stages_ms"]


class TestBudgetChecker:

    def test_no_violation_within_budget(self):
        ts = FrameTimestamp(frame_id=1, capture_ns=now_ns())
        ts.preprocess_ms = 2.0
        ts.inference_ms  = 5.0
        ts.postproc_ms   = 1.0
        ts.tracking_ms   = 1.5
        ts.smoothing_ms  = 0.3
        ts.privacy_ms    = 1.5
        ts.publish_ms    = 0.5
        result = check_budget(ts)
        assert result is None

    def test_inference_over_budget_flagged(self):
        ts = FrameTimestamp(frame_id=1, capture_ns=now_ns())
        ts.inference_ms = 20.0   # Way over 6 ms budget
        result = check_budget(ts)
        assert result is not None
        assert "inference" in result

    def test_total_over_budget_flagged(self):
        ts = FrameTimestamp(frame_id=1, capture_ns=now_ns())
        ts.preprocess_ms = 5.0
        ts.inference_ms  = 15.0
        ts.postproc_ms   = 5.0
        ts.tracking_ms   = 10.0
        ts.smoothing_ms  = 5.0
        ts.privacy_ms    = 5.0
        ts.publish_ms    = 5.0
        result = check_budget(ts)
        assert result is not None
        assert "TOTAL" in result
