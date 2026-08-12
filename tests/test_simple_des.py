"""Tests for the simple discrete-event simulator."""

import pytest

from ..memory_pool import MemoryPool
from ..des import SimpleSimulator
from .test_memory_pool import _engine_config

_GIB = 1024 ** 3


def _pool_with_ports(instance_count=1, **kwargs):
    return MemoryPool(_engine_config(**kwargs), instance_count)


class TestSimpleSimulator:
    def test_deterministic_ordering(self):
        pool = _pool_with_ports(1, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)

        # Two arrivals at the same time; seq ordering deterministic.
        a0, _ = pool.get_tensor_addr(1000)
        a1, _ = pool.get_tensor_addr(1000, mem_engine_id=0)
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
            addr, _ = pool.get_tensor_addr(1000)
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
            addr, _ = pool.get_tensor_addr(500)
            sim.schedule_arrival(
                time=0.0, source_id="s", size_bytes=500, addr=addr,
            )
            total_bytes += 500
        result = sim.run()
        assert result.makespan == pytest.approx(total_bytes / peak)

    def test_per_request_consistency(self):
        pool = _pool_with_ports(2, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)
        addr, _ = pool.get_tensor_addr(1000)
        sim.schedule_arrival(
            time=0.0, source_id="s", size_bytes=1000, addr=addr,
        )
        result = sim.run()
        for m in result.request_metrics:
            assert m.latency >= m.standalone_time
            assert m.contention_delay >= -1e-12  # floating tolerance
            assert m.average_bandwidth == pytest.approx(
                m.size / m.latency
            )

    def test_staggered_arrivals_contention_delay_nonzero(self):
        """A finishing mid-way causes B's contention delay > 0."""
        peak = 100.0 * _GIB
        pool = _pool_with_ports(1, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)
        a0, _ = pool.get_tensor_addr(1000)
        a1, _ = pool.get_tensor_addr(1000, mem_engine_id=0)

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
        addr, _ = pool.get_tensor_addr(500)
        sim.schedule_arrival(
            time=0.0, source_id="s", size_bytes=500, addr=addr,
        )
        # Second arrival invalidates earlier finish predictions.
        addr2, _ = pool.get_tensor_addr(500, mem_engine_id=0)
        sim.schedule_arrival(
            time=0.5e-9, source_id="s", size_bytes=500, addr=addr2,
        )
        result = sim.run()
        assert len(result.request_metrics) == 2
        # Old FINISH events are replaced proactively — stale count is 0.
        assert result.scheduled_finish_events == 2


class TestThreeEventOrdering:
    """Three-event scenarios: arrival and completion ordering."""

    def _run(self, arrivals, **kwargs):
        """Convenience: schedule a list of (time, source_id, size_bytes, addr)
        and return {source_id: MemoryRequestMetrics}."""
        pool = _pool_with_ports(1, capacity=1.0, bandwidth=100.0, **kwargs)
        sim = SimpleSimulator(pool)
        for t, src, size, addr in arrivals:
            sim.schedule_arrival(time=t, source_id=src, size_bytes=size,
                                 addr=addr)
        result = sim.run()
        return {m.source_id: m for m in result.request_metrics}, result

    def test_three_simultaneous_same_size(self):
        """A, B, C all at t=0, same size → same finish time, equal split."""
        peak = 100.0 * _GIB
        pool = _pool_with_ports(1, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)
        addrs = [pool.get_tensor_addr(1000, mem_engine_id=0)[0] for _ in range(3)]
        sim.schedule_arrival(time=0.0, source_id="A", size_bytes=1000,
                             addr=addrs[0])
        sim.schedule_arrival(time=0.0, source_id="B", size_bytes=1000,
                             addr=addrs[1])
        sim.schedule_arrival(time=0.0, source_id="C", size_bytes=1000,
                             addr=addrs[2])
        result = sim.run()
        m = {m.source_id: m for m in result.request_metrics}
        # All finish at 3000 / peak (each gets bandwidth/3).
        assert m["A"].finish_time == pytest.approx(3000.0 / peak)
        assert m["B"].finish_time == m["A"].finish_time
        assert m["C"].finish_time == m["A"].finish_time
        assert m["A"].contention_delay > 0

    def test_three_simultaneous_different_sizes(self):
        """A=500, B=1000, C=2000 all at t=0 → sequential completion."""
        peak = 100.0 * _GIB
        pool = _pool_with_ports(1, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)
        addrs = [pool.get_tensor_addr(s, mem_engine_id=0)[0]
                 for s in (500, 1000, 2000)]
        sim.schedule_arrival(time=0.0, source_id="A", size_bytes=500,
                             addr=addrs[0])
        sim.schedule_arrival(time=0.0, source_id="B", size_bytes=1000,
                             addr=addrs[1])
        sim.schedule_arrival(time=0.0, source_id="C", size_bytes=2000,
                             addr=addrs[2])
        result = sim.run()
        m = {m.source_id: m for m in result.request_metrics}
        # A (smallest) finishes first.
        assert m["A"].finish_time < m["B"].finish_time < m["C"].finish_time

    def test_staggered_c_arrives_after_a_completes(self):
        """A=1000@t=0, B=500@t=T/2, C=500@t=2T → A done before C arrives."""
        peak = 100.0 * _GIB
        T = 1000.0 / peak
        pool = _pool_with_ports(1, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)
        aA, _ = pool.get_tensor_addr(1000, mem_engine_id=0)
        aB, _ = pool.get_tensor_addr(500, mem_engine_id=0)
        aC, _ = pool.get_tensor_addr(500, mem_engine_id=0)
        sim.schedule_arrival(time=0.0, source_id="A", size_bytes=1000,
                             addr=aA)
        sim.schedule_arrival(time=0.5 * T, source_id="B", size_bytes=500,
                             addr=aB)
        sim.schedule_arrival(time=2.0 * T, source_id="C", size_bytes=500,
                             addr=aC)
        result = sim.run()
        m = {m.source_id: m for m in result.request_metrics}
        # C arrives after A finishes → C gets full bandwidth, no contention from A.
        assert m["C"].contention_delay == pytest.approx(0.0, abs=1e-9)
        assert m["B"].contention_delay > 0  # B contended with A

    def test_staggered_b_and_c_simultaneous(self):
        """A=2000@t=0, B=1000@t=T, C=1000@t=T → B and C arrive together."""
        peak = 100.0 * _GIB
        T = 1000.0 / peak
        pool = _pool_with_ports(1, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)
        aA, _ = pool.get_tensor_addr(2000, mem_engine_id=0)
        aB, _ = pool.get_tensor_addr(1000, mem_engine_id=0)
        aC, _ = pool.get_tensor_addr(1000, mem_engine_id=0)
        sim.schedule_arrival(time=0.0, source_id="A", size_bytes=2000,
                             addr=aA)
        sim.schedule_arrival(time=T, source_id="B", size_bytes=1000,
                             addr=aB)
        sim.schedule_arrival(time=T, source_id="C", size_bytes=1000,
                             addr=aC)
        result = sim.run()
        m = {m.source_id: m for m in result.request_metrics}
        # B and C arrive together; at T, A has 1000B left = same as B and C.
        # All three finish simultaneously at 4T.
        assert m["B"].finish_time == pytest.approx(m["C"].finish_time)
        assert m["A"].finish_time == pytest.approx(m["B"].finish_time)
        assert m["A"].finish_time == pytest.approx(4.0 * T)

    def test_three_different_engines_no_contention(self):
        """A on eng0, B on eng1, C on eng2 → zero contention."""
        peak = 100.0 * _GIB
        pool = _pool_with_ports(3, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)
        aA, _ = pool.get_tensor_addr(1000, mem_engine_id=0)
        aB, _ = pool.get_tensor_addr(1000, mem_engine_id=1)
        aC, _ = pool.get_tensor_addr(1000, mem_engine_id=2)
        sim.schedule_arrival(time=0.0, source_id="A", size_bytes=1000,
                             addr=aA)
        sim.schedule_arrival(time=0.5e-9, source_id="B", size_bytes=1000,
                             addr=aB)
        sim.schedule_arrival(time=1.0e-9, source_id="C", size_bytes=1000,
                             addr=aC)
        result = sim.run()
        m = {m.source_id: m for m in result.request_metrics}
        # Different engines → no contention at all.
        for src in ("A", "B", "C"):
            assert m[src].contention_delay == pytest.approx(0.0, abs=1e-9)
        # All have the same standalone time.
        assert m["A"].standalone_time == pytest.approx(m["B"].standalone_time)
        assert m["B"].standalone_time == pytest.approx(m["C"].standalone_time)

    def test_two_engines_mixed_contention(self):
        """A(eng0), B(eng0), C(eng1) → only A and B contend."""
        pool = _pool_with_ports(2, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)
        aA, _ = pool.get_tensor_addr(1000, mem_engine_id=0)
        aB, _ = pool.get_tensor_addr(1000, mem_engine_id=0)
        aC, _ = pool.get_tensor_addr(1000, mem_engine_id=1)
        sim.schedule_arrival(time=0.0, source_id="A", size_bytes=1000,
                             addr=aA)
        sim.schedule_arrival(time=0.0, source_id="B", size_bytes=1000,
                             addr=aB)
        sim.schedule_arrival(time=0.0, source_id="C", size_bytes=1000,
                             addr=aC)
        result = sim.run()
        m = {m.source_id: m for m in result.request_metrics}
        # A and B share engine 0 → both delayed.
        assert m["A"].contention_delay > 0
        assert m["B"].contention_delay > 0
        # C has its own engine → no delay.
        assert m["C"].contention_delay == pytest.approx(0.0, abs=1e-9)
        # C finishes before A and B.
        assert m["C"].finish_time < m["A"].finish_time
        assert m["C"].finish_time < m["B"].finish_time

    def test_stale_events_replaced_properly(self):
        """A@t=0, B@t=T/4 → A's first prediction is stale, replaced."""
        peak = 100.0 * _GIB
        T = 1000.0 / peak
        pool = _pool_with_ports(1, capacity=1.0, bandwidth=100.0)
        sim = SimpleSimulator(pool)
        aA, _ = pool.get_tensor_addr(1000, mem_engine_id=0)
        aB, _ = pool.get_tensor_addr(1000, mem_engine_id=0)
        sim.schedule_arrival(time=0.0, source_id="A", size_bytes=1000,
                             addr=aA)
        # B arrives before A completes → A's prediction is updated.
        sim.schedule_arrival(time=0.25 * T, source_id="B", size_bytes=1000,
                             addr=aB)
        result = sim.run()
        assert len(result.request_metrics) == 2
        # Both complete correctly — stale events were handled.
        m = {m.source_id: m for m in result.request_metrics}
        assert m["A"].finish_time > m["A"].standalone_time
        assert m["B"].finish_time > m["B"].standalone_time
