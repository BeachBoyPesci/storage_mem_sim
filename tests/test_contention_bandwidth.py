"""Tests for event-driven bandwidth competition engine."""

import pytest

from ..memory_request import MemoryRequest
from ..memory_type import MemoryRequestType
from ..memory_config import MemoryEngineConfig
from ..memory_type import MemoryType
from ..media import MediaConfig, MediaSystemBackend

_GIB = 1024 ** 3


def _engine(bandwidth=100.0):
    cfg = MemoryEngineConfig(
        memory_type=MemoryType.HBM,
        media_config=MediaConfig(
            media_type=MediaSystemBackend.ANALYTIC,
            capacity=1.0, bandwidth=bandwidth,
        ),
    )
    from ..memory_engine import MemoryEngine
    return MemoryEngine(cfg)


def _access(request_id, addr=0, size=1000, source_id="s0"):
    return MemoryRequest(
        addr, size, MemoryRequestType.KREAD,
        request_id=request_id, source_id=source_id,
    )


class TestSubmit:
    def test_single_request(self):
        eng = _engine()
        peak = 100.0 * _GIB
        entries = eng.submit(_access("r1", size=1000), now=0.0)
        assert len(entries) == 1
        m = entries[0].metrics
        assert m.request_id == "r1"
        assert m.finish_time == pytest.approx(1000.0 / peak)
        assert m.contention_delay == pytest.approx(0.0, abs=1e-9)

    def test_two_simultaneous_equal_split(self):
        eng = _engine()
        peak = 100.0 * _GIB
        eng.submit(_access("r1", size=1000), now=0.0)
        entries = eng.submit(_access("r2", size=1000), now=0.0)
        assert len(entries) == 2
        for req in entries:
            m = req.metrics
            assert m.finish_time == pytest.approx(2000.0 / peak)
            assert m.contention_delay > 0

    def test_returns_all_active(self):
        eng = _engine()
        eng.submit(_access("r1"), now=0.0)
        eng.submit(_access("r2"), now=0.0)
        entries = eng.submit(_access("r3"), now=0.0)
        assert len(entries) == 3

    def test_no_competition_keeps_old(self):
        """New arrival after existing finish → only new prediction returned."""
        eng = _engine()
        peak = 100.0 * _GIB
        eng.submit(_access("r1", size=100), now=0.0)
        entries = eng.submit(_access("r2", size=100), now=2 * 100.0 / peak)
        assert len(entries) == 1
        assert entries[0].metrics.request_id == "r2"

    def test_competition_returns_affected(self):
        """New arrival during active period → all affected returned."""
        eng = _engine()
        eng.submit(_access("r1", size=1000), now=0.0)
        entries = eng.submit(_access("r2", size=1000), now=0.0)
        assert len(entries) == 2
        ids = {req.metrics.request_id for req in entries}
        assert ids == {"r1", "r2"}


class TestMetrics:
    def test_metrics_on_predictions(self):
        eng = _engine()
        entries = eng.submit(_access("r1", size=500), now=0.0)
        m = entries[0].metrics
        assert m.size == 500
        assert m.latency > 0
        assert m.average_bandwidth > 0

    def test_contention_delay_zero_without_contention(self):
        eng = _engine()
        entries = eng.submit(_access("r1", size=1000), now=0.0)
        assert entries[0].metrics.contention_delay == pytest.approx(0.0, abs=1e-9)

    def test_contention_delay_positive_with_contention(self):
        eng = _engine()
        eng.submit(_access("r1", size=1000), now=0.0)
        entries = eng.submit(_access("r2", size=1000), now=0.0)
        for req in entries:
            assert req.metrics.contention_delay > 0


class TestInvariants:
    def test_makespan_equals_total_over_peak(self):
        eng = _engine()
        peak = 100.0 * _GIB
        entries = None
        for i in range(4):
            entries = eng.submit(_access(f"r{i}", size=500), now=0.0)
        # All at t=0 with equal split: each gets peak/4.
        # 500 / (peak/4) = 2000/peak = total_bytes / peak.
        assert entries is not None
        max_ft = max(req.metrics.finish_time for req in entries)
        assert max_ft == pytest.approx(2000.0 / peak)

    def test_remaining_bytes_non_negative(self):
        eng = _engine()
        eng.submit(_access("r1", size=100), now=0.0)
        for i in range(10):
            eng.submit(_access(f"rx{i}", size=10), now=i * 1e-9)


class TestFullSnapshotRegression:
    """Full-snapshot returns (every state change reports all active
    requests) — regression for the premature-completion bug of the
    tied-earliest-only era: an in-flight request whose share shrank must be
    re-predicted in the very snapshot that shrank it, so its FINISH event is
    refreshed instead of firing at the stale time."""

    def test_midflight_arrival_repredicts_all_in_snapshot(self):
        eng = _engine()
        peak = 100.0 * _GIB
        t = 1000.0 / peak
        p = eng.submit(_access("r1", size=1000), now=0.0)
        assert [e.metrics.request_id for e in p] == ["r1"]
        assert p[0].metrics.finish_time == pytest.approx(t)
        # r2 (60 B) arrives at 0.9t: r1 has 100 B left, both get half the
        # bandwidth.  r1's finish moves to 1.1t, r2 finishes at 1.02t.
        # The snapshot reports BOTH — r1's stale event @t is superseded.
        q = eng.submit(_access("r2", size=60), now=0.9 * t)
        by_id = {e.metrics.request_id: e for e in q}
        assert set(by_id) == {"r1", "r2"}
        assert by_id["r1"].metrics.finish_time == pytest.approx(1.1 * t)
        assert by_id["r2"].metrics.finish_time == pytest.approx(1.02 * t)

    def test_snapshot_insertion_order_and_work_conserving(self):
        """All equal arrivals: every submit returns the full active set in
        insertion order; makespan == total_bytes / peak."""
        eng = _engine()
        peak = 100.0 * _GIB
        head = None
        for i in range(4):
            head = eng.submit(_access(f"r{i}", size=500), now=0.0)
        assert head is not None
        assert [e.metrics.request_id for e in head] == ["r0", "r1", "r2", "r3"]
        fts = {e.metrics.finish_time for e in head}
        assert len(fts) == 1
        assert fts.pop() == pytest.approx(2000.0 / peak)

    def test_no_refusal_protocol_needed(self):
        """A FINISH at the latest prediction always pops: finishing a
        request returns the survivors' snapshot (empty when the engine goes
        idle) — no same-rid return ever occurs."""
        eng = _engine()
        peak = 100.0 * _GIB
        t = 100.0 / peak
        eng.submit(_access("r1", size=100), now=0.0)
        eng.submit(_access("r2", size=200), now=0.0)
        # r1's latest prediction: 100 / (peak/2) = 2t.  At 2t r2 still has
        # 100 B left (200 - 100 consumed), so it survives the sweep.
        entries = eng.finish("r1", 2 * t)
        assert len(entries) == 1
        assert entries[0].metrics.request_id == "r2"
        assert entries[0].metrics.finish_time == pytest.approx(3 * t)
        assert eng.finish("r2", 3 * t) == []
