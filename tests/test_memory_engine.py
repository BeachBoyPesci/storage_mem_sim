"""Tests for MemoryEngine: address allocation, request construction, metrics."""

import unittest
import sys
import os
import tempfile

from ..memory_type import MemoryType, MemoryRequestType
from ..memory_config import MemoryEngineConfig
from ..memory_request import MemoryRequest
from ..memory_engine import MemoryEngine
from ..memory_metrics import MemoryMetrics, MemoryEngineMetrics
from ..media import (
    BaseMediaSystem,
    MediaConfig,
    MediaMetrics,
    MediaSystemBackend,
)


class TestMemoryEngineAddressAllocation(unittest.TestCase):
    """Test MemoryEngine address management."""

    def setUp(self):
        self.engine = MemoryEngine(MemoryEngineConfig(
            memory_type=MemoryType.HBM,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                capacity=1.0,  # 1 GB → per_dp_capacity = 1GB
                bandwidth=100.0,
            ),
        ))

    def test_align_up(self):
        g = self.engine.mem_config.granularity
        self.assertEqual(self.engine.align_up(1), g)
        self.assertEqual(self.engine.align_up(g - 1), g)
        self.assertEqual(self.engine.align_up(g), g)
        self.assertEqual(self.engine.align_up(g + 1), 2 * g)
        self.assertEqual(self.engine.align_up(0), 0)

    def test_get_tensor_addr_sequential(self):
        g = self.engine.mem_config.granularity
        addr1 = self.engine.get_tensor_addr(g)
        addr2 = self.engine.get_tensor_addr(g)
        self.assertEqual(addr1, 0)
        self.assertEqual(addr2, g)

    def test_get_tensor_addr_alignment(self):
        g = self.engine.mem_config.granularity
        addr = self.engine.get_tensor_addr(100)
        self.assertEqual(addr, 0)
        addr2 = self.engine.get_tensor_addr(g)
        self.assertEqual(addr2, self.engine.align_up(100))

    def test_capacity_overflow(self):
        cap = self.engine.mem_config.per_dp_capacity
        self.engine.get_tensor_addr(cap)
        self.engine.reset_addr()
        with self.assertRaises(OverflowError):
            self.engine.get_tensor_addr(cap + 1)

    def test_reset_addr(self):
        g = self.engine.mem_config.granularity
        self.engine.get_tensor_addr(g * 100)
        self.engine.reset_addr()
        addr = self.engine.get_tensor_addr(g)
        self.assertEqual(addr, 0)


class TestMemoryEngineIssueRequest(unittest.TestCase):
    """Test MemoryEngine issue_request flow."""

    def setUp(self):
        self.engine = MemoryEngine(MemoryEngineConfig(
            memory_type=MemoryType.HBM,
            dp_size=1,
            storage_instance_num=1,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                capacity=1.0,  # 1 GB
                bandwidth=100.0,
            ),
        ))

    def test_issue_request_no_media_system_raises(self):
        """Engine without media_config raises ValueError at construction."""
        with self.assertRaises(ValueError):
            MemoryEngine(MemoryEngineConfig())

    def test_issue_request_single(self):
        metrics = self.engine.issue_request(
            [0], [64], [MemoryRequestType.KREAD]
        )
        self.assertIsInstance(metrics, MemoryMetrics)
        self.assertGreater(metrics.total_time, 0)

    def test_issue_request_multiple(self):
        metrics = self.engine.issue_request(
            [0, 64, 128],
            [64, 128, 256],
            [MemoryRequestType.KREAD, MemoryRequestType.KWRITE, MemoryRequestType.KREAD],
        )
        self.assertEqual(metrics.memory_reqs_num, 3)  # Analytic: no decomposition

    def test_dp_size_gt1_deprecated_warns_and_ignored(self):
        """dp_size > 1 warns and is ignored (no replication anymore)."""
        with self.assertWarns(DeprecationWarning):
            config = MemoryEngineConfig(
                dp_size=2,
                media_config=MediaConfig(
                    media_type=MediaSystemBackend.ANALYTIC,
                    capacity=1.0,
                    bandwidth=100.0,
                ),
            )
        engine = MemoryEngine(config)
        metrics = engine.issue_request(
            [0], [64], [MemoryRequestType.KREAD]
        )
        self.assertEqual(metrics.memory_reqs_num, 1)
        self.assertEqual(metrics.global_memory_reqs_num, 1)

    def test_storage_instance_num_gt1_deprecated_warns(self):
        with self.assertWarns(DeprecationWarning):
            MemoryEngineConfig(
                storage_instance_num=2,
                media_config=MediaConfig(
                    media_type=MediaSystemBackend.ANALYTIC,
                    capacity=1.0,
                    bandwidth=100.0,
                ),
            )

    def test_global_memory_reqs_num_equals_local(self):
        """global_memory_reqs_num is always equal to memory_reqs_num."""
        metrics = self.engine.issue_request(
            [0, 64], [64, 64],
            [MemoryRequestType.KREAD, MemoryRequestType.KWRITE],
        )
        self.assertEqual(metrics.memory_reqs_num, 2)
        self.assertEqual(metrics.global_memory_reqs_num, 2)

    def test_engine_metrics_accumulation(self):
        self.engine.issue_request([0], [64], [MemoryRequestType.KREAD])
        self.engine.issue_request([0], [128], [MemoryRequestType.KWRITE])
        em = self.engine.get_engine_metrics()
        self.assertEqual(len(em.mem_metrics_list), 2)
        self.assertEqual(em.memory_reqs_num, 2)  # 2 engine-level requests

    def test_reset_engine_metrics(self):
        self.engine.issue_request([0], [64], [MemoryRequestType.KREAD])
        self.engine.reset_engine_metrics()
        em = self.engine.get_engine_metrics()
        self.assertEqual(len(em.mem_metrics_list), 0)


class _StubNonAnalyticMediaSystem(BaseMediaSystem):
    """Stub backend with a non-Analytic type, without building Ramulator."""

    def __init__(self):
        super().__init__(MediaConfig(
            media_type=MediaSystemBackend.RAMULATOR, bandwidth=100.0))

    def handler_mem_request(self, mem_req_list):
        return MediaMetrics()


class TestMemoryEngineEventPort(unittest.TestCase):
    """Test the event port: creation, mode latch, metric isolation."""

    def setUp(self):
        self.engine = MemoryEngine(MemoryEngineConfig(
            memory_type=MemoryType.HBM,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                capacity=1.0,
                bandwidth=100.0,
            ),
        ))

    def _access(self, request_id="r1", size_bytes=64):
        return MemoryRequest(0, size_bytes, MemoryRequestType.KREAD,
                             request_id=request_id, source_id="s0")

    def test_sync_then_event_no_error(self):
        """Sync and event paths can interleave without error."""
        self.engine.issue_request([0], [64], [MemoryRequestType.KREAD])
        self.engine.submit(self._access(), now=0.0)

    def test_event_then_sync_no_error(self):
        """Event and sync paths can interleave without error."""
        self.engine.submit(self._access(), now=0.0)
        self.engine.issue_request([0], [64], [MemoryRequestType.KREAD])

    def test_event_does_not_pollute_sync_metrics(self):
        """Event activity leaves the sync accumulator untouched."""
        peak = 100.0 * (1024 ** 3)
        now = 64.0 / peak
        entries = self.engine.submit(
            self._access(size_bytes=64), now=now)
        # Sync metrics should be untouched.
        em = self.engine.get_engine_metrics()
        self.assertEqual(em.total_bytes, 0)
        # Event submit returns prediction with metrics.
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].metrics.size, 64)


class TestMemoryEngineConfig(unittest.TestCase):
    """Test MemoryEngineConfig validation."""

    def test_default_config(self):
        config = MemoryEngineConfig()
        self.assertEqual(config.memory_type, MemoryType.HBM)
        self.assertEqual(config.dp_size, 1)
        self.assertEqual(config.storage_instance_num, 1)

    def test_default_config_does_not_warn(self):
        """Default values must not emit deprecation warnings."""
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            MemoryEngineConfig()

    def test_single_instance_capacity_derivation(self):
        """capacity is per-instance now (no division by dp/instances)."""
        config = MemoryEngineConfig(
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                capacity=2.0,
                bandwidth=100.0,
            ),
        )
        self.assertEqual(config.total_capacity, 2 * 1024 ** 3)
        self.assertEqual(config.per_dp_capacity, 2 * 1024 ** 3)
        self.assertEqual(config.capacity, 2 * 1024 ** 3)

    def test_invalid_dp_size(self):
        with self.assertRaises(ValueError):
            MemoryEngineConfig(dp_size=0)

    def test_invalid_storage_instance_num(self):
        with self.assertRaises(ValueError):
            MemoryEngineConfig(storage_instance_num=0)


class TestMemoryEngineMetrics(unittest.TestCase):
    """Test MemoryMetrics and MemoryEngineMetrics."""

    def test_memory_metrics_default(self):
        m = MemoryMetrics()
        self.assertEqual(m.cycles, 0)
        self.assertEqual(m.total_time, 0.0)

    def test_engine_metrics_update(self):
        em = MemoryEngineMetrics()
        m = MemoryMetrics(cycles=100, total_time=0.5, memory_reqs_num=4)
        em.update(m, total_bytes=256)
        self.assertEqual(em.cycles, 100)
        self.assertEqual(em.total_time, 0.5)
        self.assertEqual(em.memory_reqs_num, 4)
        self.assertEqual(len(em.mem_metrics_list), 1)

    def test_engine_metrics_bandwidth_from_total_bytes(self):
        """bandwidth = total_bytes / total_time (exact, no time-weight needed)."""
        em = MemoryEngineMetrics()
        m = MemoryMetrics(cycles=0, total_time=2.0, memory_reqs_num=10,
                          bandwidth=999, iops=999)
        em.update(m, total_bytes=1000)
        self.assertEqual(em.bandwidth, 500.0)  # 1000 / 2.0

    def test_engine_metrics_iops_time_weighted(self):
        """MQSim device IOPS is time-weighted across simulation batches."""
        em = MemoryEngineMetrics()
        m1 = MemoryMetrics(cycles=0, total_time=0.1, memory_reqs_num=1,
                           global_memory_reqs_num=100,
                           iops=1000.0, iops_read=800.0, iops_write=200.0,
                           bandwidth=500.0)
        em.update(m1, total_bytes=50)
        self.assertEqual(em.iops, 1000.0)
        self.assertEqual(em.iops_read, 800.0)
        self.assertEqual(em.iops_write, 200.0)

        m2 = MemoryMetrics(cycles=0, total_time=0.2, memory_reqs_num=1,
                           global_memory_reqs_num=100,
                           iops=500.0, iops_read=100.0, iops_write=400.0,
                           bandwidth=250.0)
        em.update(m2, total_bytes=50)
        self.assertAlmostEqual(em.iops, 666.666, places=1)
        self.assertAlmostEqual(em.iops_read, 333.333, places=1)
        self.assertAlmostEqual(em.iops_write, 333.333, places=1)

    def test_engine_metrics_without_device_iops_remains_none(self):
        """Analytic/Ramulator metrics do not synthesize logical IOPS."""
        em = MemoryEngineMetrics()
        m = MemoryMetrics(cycles=0, total_time=0.5, memory_reqs_num=1,
                          global_memory_reqs_num=100)
        em.update(m, total_bytes=1000)
        self.assertIsNone(em.iops)
        self.assertIsNone(em.iops_read)
        self.assertIsNone(em.iops_write)


# ------------------------------------------------------------------
# MemoryEngine + MQSim integration tests
# ------------------------------------------------------------------

_mqsim_available = False
try:
    from ..media.mqsim_wrapper.pymqsim import check_mqsim_available
    _mqsim_available = check_mqsim_available()
except Exception:
    pass


@unittest.skipUnless(_mqsim_available, "MQSim engine not built")
class TestMemoryEngineWithMQSim(unittest.TestCase):
    """Integration tests: MemoryEngine with MQSim backend."""

    def setUp(self):
        cfg_dir = os.path.join(
            os.path.dirname(__file__), "..", "configs", "mqsim"
        )
        ssd_config = os.path.join(cfg_dir, "default_ssdconfig.xml")
        workload_config = os.path.join(cfg_dir, "default_workload.xml")
        self.engine = MemoryEngine(MemoryEngineConfig(
            memory_type=MemoryType.SSD,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.MQSIM,
                capacity=512.0,
                ssd_config_path=os.path.abspath(ssd_config),
                workload_config_path=os.path.abspath(workload_config),
                merge_contiguous=True,
                request_size_bytes=131072,
            ),
        ))

    def test_sequential_read_metrics(self):
        """Sequential reads: bandwidth > 0, iops > 0, time > 0."""
        n = 8
        request_size = 131072  # 128 KB
        total_bytes = request_size * n
        base_addr = self.engine.get_tensor_addr(total_bytes)
        addrs = [base_addr + i * request_size for i in range(n)]
        sizes = [request_size] * n

        metrics = self.engine.issue_request(
            addrs, sizes, [MemoryRequestType.KREAD] * n,
        )

        self.assertGreater(metrics.total_time, 0, "Simulation time should be > 0")
        self.assertGreater(metrics.bandwidth, 0, "Bandwidth should be > 0")
        self.assertGreater(metrics.iops, 0, "IOPS should be > 0")
        # bandwidth * time ≈ total_bytes (within ~30%)
        computed_bytes = metrics.bandwidth * metrics.total_time
        ratio = computed_bytes / total_bytes
        self.assertAlmostEqual(ratio, 1.0, delta=0.3,
            msg=f"BW*time={computed_bytes:.0f} vs total={total_bytes}")

    def test_engine_metrics_accumulation(self):
        """Two batches: cumulative metrics accumulate correctly."""
        request_size = 65536  # 64 KB
        n_per_batch = 4
        total_bytes_per_batch = request_size * n_per_batch

        for batch in range(2):
            base_addr = self.engine.get_tensor_addr(total_bytes_per_batch)
            addrs = [base_addr + i * request_size for i in range(n_per_batch)]
            sizes = [request_size] * n_per_batch
            self.engine.issue_request(
                addrs, sizes, [MemoryRequestType.KREAD] * n_per_batch,
            )

        em = self.engine.get_engine_metrics()
        self.assertEqual(len(em.mem_metrics_list), 2)
        self.assertEqual(em.total_bytes, total_bytes_per_batch * 2)
        self.assertGreater(em.total_time, 0)
        self.assertGreater(em.bandwidth, 0)
        self.assertGreater(em.iops, 0)


if __name__ == "__main__":
    unittest.main()
