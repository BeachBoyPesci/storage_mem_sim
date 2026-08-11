"""Tests for the simple discrete-event simulator."""

import pytest

from ..memory_pool import MemoryAccess, MemoryPool
from ..des import SimpleSimulator
from .test_memory_pool import _engine_config

_GIB = 1024 ** 3


def _pool_with_ports(instance_count=1, **kwargs):
    return MemoryPool(instance_count, _engine_config(**kwargs))


class TestSimpleSimulator:
    def test_deterministic_ordering(self):
        pool = _pool_with_ports(1, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)

        # Two arrivals at the same time; seq ordering deterministic.
        a0 = pool.get_tensor_addr(1000)
        a1 = pool.get_tensor_addr(1000, mem_engine_id=0)
        _ = sim.schedule_arrival(
            time=0.0, source_id="sA", size_bytes=1000, addr=a0)
        _ = sim.schedule_arrival(
            time=0.0, source_id="sB", size_bytes=1000, addr=a1)
        result = sim.run()
        assert len(result.request_metrics) == 2
        assert result.makespan > 0

    def test_empty_queue_terminates(self):
        pool = _pool_with_ports(1, capacity=1.0)
        sim = SimpleSimulator(pool)
        result = sim.run()
        assert result.request_metrics == []
        assert result.makespan == 0.0

    def test_multi_source_aggregation(self):
        pool = _pool_with_ports(1, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)
        for i in range(3):
            addr = pool.get_tensor_addr(1000)
            sim.schedule_arrival(
                time=i * 1e-9, source_id=f"src{i%2}",
                size_bytes=1000, addr=addr,
            )
        result = sim.run()
        sources = set(k for k in result.per_source)
        assert "src0" in sources
        assert "src1" in sources
        for s, stats in result.per_source.items():
            assert stats["avg_latency"] > 0
            assert stats["avg_contention_delay"] >= 0
            assert stats["total_bytes"] > 0
            assert stats["count"] > 0

    def test_makespan_equals_total_over_peak(self):
        peak = 100.0 * _GIB
        pool = _pool_with_ports(1, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)
        total_bytes = 0
        for i in range(4):
            addr = pool.get_tensor_addr(500)
            sim.schedule_arrival(
                time=0.0, source_id="s", size_bytes=500, addr=addr,
            )
            total_bytes += 500
        result = sim.run()
        assert result.makespan == pytest.approx(total_bytes / peak)

    def test_per_request_consistency(self):
        pool = _pool_with_ports(2, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)
        addr = pool.get_tensor_addr(1000)
        sim.schedule_arrival(
            time=0.0, source_id="s", size_bytes=1000, addr=addr,
        )
        result = sim.run()
        for m in result.request_metrics:
            assert m.latency >= m.standalone_time
            assert m.contention_delay >= -1e-12  # floating tolerance
            assert m.average_bandwidth == pytest.approx(
                m.size_bytes / m.latency
            )

    def test_staggered_arrivals_contention_delay_nonzero(self):
        """A finishing mid-way causes B's contention delay > 0."""
        peak = 100.0 * _GIB
        pool = _pool_with_ports(1, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)
        a0 = pool.get_tensor_addr(1000)
        a1 = pool.get_tensor_addr(1000, mem_engine_id=0)

        # A arrives at t=0, B arrives halfway through A's standalone time.
        standalone = 1000.0 / peak
        sim.schedule_arrival(
            time=0.0, source_id="sA", size_bytes=1000, addr=a0)
        sim.schedule_arrival(
            time=0.5 * standalone, source_id="sB", size_bytes=1000, addr=a1)

        result = sim.run()
        metrics_by_src = {m.source_id: m for m in result.request_metrics}
        assert metrics_by_src["sA"].contention_delay > 0
        assert metrics_by_src["sB"].contention_delay > 0


class TestSimulatorStaleEvents:
    def test_new_arrival_replaces_stale_finish(self):
        pool = _pool_with_ports(1, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)
        addr = pool.get_tensor_addr(500)
        sim.schedule_arrival(
            time=0.0, source_id="s", size_bytes=500, addr=addr,
        )
        # Second arrival invalidates earlier finish predictions.
        addr2 = pool.get_tensor_addr(500, mem_engine_id=0)
        sim.schedule_arrival(
            time=0.5e-9, source_id="s", size_bytes=500, addr=addr2,
        )
        result = sim.run()
        assert len(result.request_metrics) == 2
        # Old FINISH events are replaced proactively — stale count is 0.
        assert result.scheduled_finish_events == 2
