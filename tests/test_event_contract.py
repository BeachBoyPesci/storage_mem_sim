"""Contract tests for the pool event API (§13.5 of the design doc)."""

import os
import subprocess
import sys

import pytest

from ..memory_type import MemoryRequestType
from ..memory_pool import Event, EventKind, MemoryPool
from ..des import SimpleSimulator
from .test_memory_pool import _engine_config

_GIB = 1024 ** 3

_pool = MemoryPool.from_homogeneous


class TestEventContract:
    def test_arrival_creates_finish_events(self):
        pool = _pool(1, _engine_config())
        sim = SimpleSimulator(pool)
        addr = pool.get_tensor_addr(1000)
        sim.schedule_arrival(
            time=0.0, source_id="s", size_bytes=1000, addr=addr,
        )
        result = sim.run()
        assert result.scheduled_finish_events == 1
        assert result.stale_finish_events == 0

    def test_new_arrival_invalidates_old_finish(self):
        pool = _pool(1, _engine_config())
        sim = SimpleSimulator(pool)
        addr1 = pool.get_tensor_addr(1000, mem_engine_id=0)
        addr2 = pool.get_tensor_addr(2000, mem_engine_id=0)
        sim.schedule_arrival(
            time=0.0, source_id="s", size_bytes=1000, addr=addr1)
        sim.schedule_arrival(
            time=0.5e-9, source_id="s", size_bytes=2000, addr=addr2)
        result = sim.run()
        assert len(result.request_metrics) == 2
        # Old FINISH events are replaced proactively in the queue;
        # stale count is always zero in the new design.

    def test_finish_returns_only_target_metrics(self):
        pool = _pool(1, _engine_config())
        sim = SimpleSimulator(pool)
        a0 = pool.get_tensor_addr(1000, mem_engine_id=0)
        a1 = pool.get_tensor_addr(2000, mem_engine_id=0)
        sim.schedule_arrival(
            time=0.0, source_id="sA", size_bytes=1000, addr=a0)
        sim.schedule_arrival(
            time=0.0, source_id="sB", size_bytes=2000, addr=a1)
        result = sim.run()
        assert len(result.request_metrics) == 2
        ids = {m.source_id for m in result.request_metrics}
        assert "sA" in ids
        assert "sB" in ids

    def test_staggered_hand_computed(self):
        """A=1000 B @ t=0, B=1000 B @ t=T/2, peak=100 GiB/s →
        A finishes at 1.5 T, B at 2.0 T (where T = size / peak).
        Cascading completion: after A finishes, B regains full bandwidth."""
        peak = 100.0 * _GIB
        T = 1000.0 / peak
        pool = _pool(1, _engine_config(bandwidth=100.0))
        sim = SimpleSimulator(pool)
        a0 = pool.get_tensor_addr(1000, mem_engine_id=0)
        a1 = pool.get_tensor_addr(1000, mem_engine_id=0)
        sim.schedule_arrival(
            time=0.0, source_id="A", size_bytes=1000, addr=a0)
        sim.schedule_arrival(
            time=0.5 * T, source_id="B", size_bytes=1000, addr=a1)
        result = sim.run()

        metrics = {m.source_id: m for m in result.request_metrics}
        assert metrics["A"].finish_time == pytest.approx(1.5 * T)
        assert metrics["B"].finish_time == pytest.approx(2.0 * T)
        assert metrics["A"].contention_delay > 0  # B's arrival halves A's bw
        assert metrics["B"].contention_delay > 0

    def test_simultaneous_makespan_equals_total_over_peak(self):
        peak = 100.0 * _GIB
        pool = _pool(2, _engine_config(bandwidth=100.0))
        sim = SimpleSimulator(pool)
        total = 0
        for i in range(6):
            addr = pool.get_tensor_addr(1000)
            sim.schedule_arrival(
                time=0.0, source_id=f"s{i%3}", size_bytes=1000, addr=addr)
            total += 1000
        result = sim.run()
        assert result.makespan == pytest.approx(total / (2 * peak))

    def test_all_request_metrics_consistent(self):
        pool = _pool(2, _engine_config(bandwidth=100.0))
        sim = SimpleSimulator(pool)
        for _ in range(4):
            addr = pool.get_tensor_addr(500)
            sim.schedule_arrival(
                time=0.0, source_id="s", size_bytes=500, addr=addr)
        result = sim.run()
        for m in result.request_metrics:
            assert m.average_bandwidth == pytest.approx(
                m.size_bytes / m.latency)
            assert m.contention_delay == pytest.approx(
                m.latency - m.standalone_time)
            assert m.contention_delay >= -1e-12

    def test_event_sync_mixing_on_same_engine_raises(self):
        pool = _pool(1, _engine_config())
        addr = pool.get_tensor_addr(64, mem_engine_id=0)
        # Use submit to lock engine to event mode.
        ev = Event(
            time=0.0, seq=0, kind=EventKind.ARRIVAL,
            source_id="s", request_id="r1",
            mem_engine_id=None, addr=addr, size_bytes=64,
            req_type=MemoryRequestType.KREAD,
        )
        pool.submit(ev)
        # Now sync issue_request() on the same engine must raise.
        engine = pool.get_engine(0)
        with pytest.raises(RuntimeError, match="locked"):
            engine.issue_request([0], [64], [MemoryRequestType.KREAD])


def test_import_boundary_no_des_in_pool():
    """Importing memory_pool must NOT pull in storage_mem_sim.des."""
    repo_parent = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    script = (
        "import sys\n"
        f"sys.path.insert(0, {repo_parent!r})\n"
        "import storage_mem_sim.memory_pool\n"
        'assert "storage_mem_sim.des" not in sys.modules, (\n'
        '    "pool imported des: "\n'
        '    + str([k for k in sorted(sys.modules) if "des" in k])\n'
        ")\n"
        "print('BOUNDARY_OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert "BOUNDARY_OK" in result.stdout, (
        f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    )
