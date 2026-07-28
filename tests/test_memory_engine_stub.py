"""Unit tests for MemoryEngine multi-DP/instance logic using a fake media system.

Does not depend on Ramulator or any real backend.
"""

import unittest
import sys
import os

from ..memory_type import MemoryType, MemoryRequestType
from ..memory_config import MemoryEngineConfig
from ..memory_object import MemoryObject
from ..memory_request import MemoryRequest
from ..memory_engine import MemoryEngine
from ..memory_metrics import MemoryMetrics, MemoryEngineMetrics
from ..media import (
    BaseMediaSystem,
    MediaConfig,
    MediaMetrics,
    MediaSystemBackend,
)


class FakeMediaSystem(BaseMediaSystem):
    """Fake media system that returns mock MediaMetrics.

    Tracks every call to handler_mem_request so tests can inspect
    the actual request list passed to the media system.
    """

    def __init__(self):
        super().__init__(MediaConfig(
            media_type=MediaSystemBackend.ANALYTIC, bandwidth=100.0))
        self.calls: list[list[MemoryRequest]] = []
        self.device_iops = None

    def handler_mem_request(self, mem_req_list):
        self.calls.append(mem_req_list)
        return MediaMetrics(
            cycles=len(mem_req_list) * 10,  # fake: 10 cycles per request
            time=len(mem_req_list) * 1e-9,
            num_media_reqs=len(mem_req_list),
            iops=self.device_iops,
        )


class TestMultiInstanceDistribution(unittest.TestCase):
    """Verify request distribution across DP ranks and storage instances."""

    def setUp(self):
        pass

    def _make_engine(self, dp_size=1, instance_num=1):
        """Create a MemoryEngine with a fresh fake media system."""
        fake = FakeMediaSystem()
        engine = MemoryEngine(MemoryEngineConfig(
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                bandwidth=100.0, capacity=1.0),
        ))
        engine.mem_config.dp_size = dp_size
        engine.mem_config.storage_instance_num = instance_num
        engine.media_system = fake
        return engine, fake

    def test_single_instance_dp1_bytes(self):
        """dp=1, inst=1, size=[64] → simulated_bytes == 64."""
        engine, fake = self._make_engine(dp_size=1, instance_num=1)
        engine.issue_request([0], [64], [MemoryRequestType.KREAD])
        self.assertEqual(engine.get_engine_metrics().total_bytes, 64)

    def test_inst2_bytes_first_instance(self):
        """dp=1, inst=2, size=[64] → only inst[0] gets the request, bytes=64."""
        engine, fake = self._make_engine(dp_size=1, instance_num=2)
        engine.issue_request([0], [64], [MemoryRequestType.KREAD])
        # 1 request → idx%2=0 → inst[0] only. inst[1] gets nothing.
        self.assertEqual(engine.get_engine_metrics().total_bytes, 64)

    def test_dp4_inst2_bytes(self):
        """dp=4, inst=2, size=[64] → simulated instance gets 128 bytes."""
        engine, fake = self._make_engine(dp_size=4, instance_num=2)

        # 1 user req × 4 dp = 4 engine reqs, round-robin:
        #   inst[0]: req0-dp0, req0-dp2  (2 requests × 64 = 128 bytes)
        #   inst[1]: req0-dp1, req0-dp3  (2 requests × 64 = 128 bytes)
        engine.issue_request([0], [64], [MemoryRequestType.KREAD])
        em = engine.get_engine_metrics()

        self.assertEqual(em.total_bytes, 128)
        self.assertEqual(len(fake.calls), 1)

    def test_different_sizes_round_robin(self):
        """Different sizes round-robin: simulated bytes = first instance's sum."""
        engine, fake = self._make_engine(dp_size=1, instance_num=2)

        # 3 requests, sizes [1024, 64, 256] → round-robin:
        #   inst[0]: req0(1024), req2(256)  → 1280 bytes
        #   inst[1]: req1(64)                → 64 bytes
        engine.issue_request(
            [0, 100, 200], [1024, 64, 256],
            [MemoryRequestType.KREAD] * 3,
        )
        self.assertEqual(engine.get_engine_metrics().total_bytes, 1280)

    def test_empty_requests(self):
        """Empty request list returns zero metrics, no calls to media system."""
        engine, fake = self._make_engine(dp_size=2, instance_num=2)
        metrics = engine.issue_request([], [], [])
        self.assertEqual(metrics.cycles, 0)
        self.assertEqual(metrics.total_time, 0.0)
        self.assertEqual(metrics.memory_reqs_num, 0)
        self.assertEqual(metrics.global_memory_reqs_num, 0)
        self.assertEqual(len(fake.calls), 0)

    def test_bandwidth_uses_simulated_bytes(self):
        """bandwidth = simulated_bytes / simulated_time."""
        engine, fake = self._make_engine(dp_size=2, instance_num=2)

        # 2 user reqs × size=100 × dp=2 = 4 engine reqs, round-robin:
        #   inst[0]: 2 reqs × 100 = 200 bytes, time = 2 * 1e-9s = 2ns
        engine.issue_request(
            [0, 100], [100, 100],
            [MemoryRequestType.KREAD, MemoryRequestType.KREAD],
        )
        em = engine.get_engine_metrics()
        expected_bw = 200.0 / 2e-9
        self.assertAlmostEqual(em.bandwidth, expected_bw, places=0)

    def test_memory_reqs_num_simulated_instance(self):
        """memory_reqs_num = requests in simulated instance."""
        engine, fake = self._make_engine(dp_size=2, instance_num=3)

        # 2 user reqs × dp=2 = 4 engine reqs, round-robin to 3 instances:
        #   inst[0]: idx 0,3 → 2 reqs
        #   inst[1]: idx 1   → 1 req
        #   inst[2]: idx 2   → 1 req
        metrics = engine.issue_request(
            [0, 100], [64, 64],
            [MemoryRequestType.KREAD, MemoryRequestType.KREAD],
        )
        self.assertEqual(metrics.memory_reqs_num, 2)
        self.assertEqual(metrics.global_memory_reqs_num, 4)

    def test_single_request_no_instance_drop(self):
        """1 request, 3 instances: request doesn't get dropped."""
        engine, fake = self._make_engine(dp_size=1, instance_num=3)
        metrics = engine.issue_request(
            [0], [64], [MemoryRequestType.KREAD],
        )
        self.assertEqual(metrics.memory_reqs_num, 1)
        self.assertEqual(metrics.global_memory_reqs_num, 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(fake.calls[0]), 1)

    def test_device_iops_is_passed_through_without_logical_recomputation(self):
        """MemoryEngine preserves the device rate returned by its backend."""
        engine, fake = self._make_engine(dp_size=2, instance_num=2)
        fake.device_iops = 12345.0

        metrics = engine.issue_request(
            [0, 64], [64, 64],
            [MemoryRequestType.KREAD, MemoryRequestType.KREAD],
        )

        self.assertEqual(metrics.iops, 12345.0)
        self.assertEqual(engine.get_engine_metrics().iops, 12345.0)


if __name__ == "__main__":
    unittest.main()
