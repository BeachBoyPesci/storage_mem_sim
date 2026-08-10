"""Tests for event-driven bandwidth competition engine."""

import pytest

from ..memory_pool import MemoryAccess
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


def _access(request_id, addr=0, size_bytes=1000, source_id="s0"):
    return MemoryAccess(
        request_id=request_id, source_id=source_id, addr=addr,
        size_bytes=size_bytes, req_type=MemoryRequestType.KREAD,
    )


class TestSubmit:
    def test_single_request(self):
        eng = _engine()
        peak = 100.0 * _GIB
        entries = eng.submit(_access("r1", size_bytes=1000), local_addr=0, now=0.0)
        assert len(entries) == 1
        rid, ft, m = entries[0]
        assert rid == "r1"
        assert ft == pytest.approx(1000.0 / peak)
        assert m.contention_delay == pytest.approx(0.0, abs=1e-9)

    def test_two_simultaneous_equal_split(self):
        eng = _engine()
        peak = 100.0 * _GIB
        eng.submit(_access("r1", size_bytes=1000), local_addr=0, now=0.0)
        entries = eng.submit(_access("r2", size_bytes=1000), local_addr=0, now=0.0)
        assert len(entries) == 2
        for rid, ft, m in entries:
            assert ft == pytest.approx(2000.0 / peak)
            assert m.contention_delay > 0

    def test_returns_all_active(self):
        eng = _engine()
        eng.submit(_access("r1"), local_addr=0, now=0.0)
        eng.submit(_access("r2"), local_addr=0, now=0.0)
        entries = eng.submit(_access("r3"), local_addr=0, now=0.0)
        assert len(entries) == 3

    def test_no_competition_keeps_old(self):
        """New arrival after existing finish → only new prediction returned."""
        eng = _engine()
        peak = 100.0 * _GIB
        eng.submit(_access("r1", size_bytes=100), local_addr=0, now=0.0)
        entries = eng.submit(_access("r2", size_bytes=100), local_addr=0,
                             now=2 * 100.0 / peak)
        assert len(entries) == 1
        assert entries[0][0] == "r2"

    def test_competition_returns_affected(self):
        """New arrival during active period → all affected returned."""
        eng = _engine()
        eng.submit(_access("r1", size_bytes=1000), local_addr=0, now=0.0)
        entries = eng.submit(_access("r2", size_bytes=1000), local_addr=0, now=0.0)
        assert len(entries) == 2
        ids = {r[0] for r in entries}
        assert ids == {"r1", "r2"}


class TestMetrics:
    def test_metrics_on_predictions(self):
        eng = _engine()
        entries = eng.submit(_access("r1", size_bytes=500), local_addr=0, now=0.0)
        _, _, m = entries[0]
        assert m.size_bytes == 500
        assert m.latency > 0
        assert m.average_bandwidth > 0

    def test_contention_delay_zero_without_contention(self):
        eng = _engine()
        entries = eng.submit(_access("r1", size_bytes=1000), local_addr=0, now=0.0)
        assert entries[0][2].contention_delay == pytest.approx(0.0, abs=1e-9)

    def test_contention_delay_positive_with_contention(self):
        eng = _engine()
        eng.submit(_access("r1", size_bytes=1000), local_addr=0, now=0.0)
        entries = eng.submit(_access("r2", size_bytes=1000), local_addr=0, now=0.0)
        for _, _, m in entries:
            assert m.contention_delay > 0


class TestInvariants:
    def test_makespan_equals_total_over_peak(self):
        eng = _engine()
        peak = 100.0 * _GIB
        entries = None
        for i in range(4):
            entries = eng.submit(_access(f"r{i}", size_bytes=500), local_addr=0, now=0.0)
        # All at t=0 with equal split: each gets peak/4.
        # 500 / (peak/4) = 2000/peak = total_bytes / peak.
        assert entries is not None
        max_ft = max(ft for _, ft, _ in entries)
        assert max_ft == pytest.approx(2000.0 / peak)

    def test_remaining_bytes_non_negative(self):
        eng = _engine()
        eng.submit(_access("r1", size_bytes=100), local_addr=0, now=0.0)
        for i in range(10):
            eng.submit(_access(f"rx{i}", size_bytes=10), local_addr=0, now=i * 1e-9)
