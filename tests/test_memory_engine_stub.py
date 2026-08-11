"""Unit tests for MemoryEngine single-instance logic using a fake media system.

Does not depend on Ramulator or any real backend. Multi-instance
distribution tests moved to test_memory_pool.py.
"""

import unittest
import sys
import os

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


class TestMemoryEngineSingleInstance(unittest.TestCase):
    """Verify single-instance request handling with a fake media system."""

    def _make_engine(self):
        """Create a MemoryEngine with a fresh fake media system."""
        fake = FakeMediaSystem()
        engine = MemoryEngine(MemoryEngineConfig(
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                bandwidth=100.0, capacity=1.0),
        ))
        engine.media_system = fake
        return engine, fake

    def test_single_request_bytes(self):
        """size=[64] → simulated_bytes == 64."""
        engine, fake = self._make_engine()
        engine.issue_request([0], [64], [MemoryRequestType.KREAD])
        self.assertEqual(engine.get_engine_metrics().total_bytes, 64)

    def test_empty_requests(self):
        """Empty request list returns zero metrics, no calls to media system."""
        engine, fake = self._make_engine()
        metrics = engine.issue_request([], [], [])
        self.assertEqual(metrics.cycles, 0)
        self.assertEqual(metrics.total_time, 0.0)
        self.assertEqual(metrics.memory_reqs_num, 0)
        self.assertEqual(metrics.global_memory_reqs_num, 0)
        self.assertEqual(len(fake.calls), 0)

    def test_bandwidth_uses_simulated_bytes(self):
        """bandwidth = simulated_bytes / simulated_time."""
        engine, fake = self._make_engine()

        # 2 requests × 100 B → 200 bytes, time = 2 * 1e-9 s = 2 ns
        engine.issue_request(
            [0, 100], [100, 100],
            [MemoryRequestType.KREAD, MemoryRequestType.KREAD],
        )
        em = engine.get_engine_metrics()
        expected_bw = 200.0 / 2e-9
        self.assertAlmostEqual(em.bandwidth, expected_bw, places=0)

    def test_device_iops_is_passed_through_without_logical_recomputation(self):
        """MemoryEngine preserves the device rate returned by its backend."""
        engine, fake = self._make_engine()
        fake.device_iops = 12345.0

        metrics = engine.issue_request(
            [0, 64], [64, 64],
            [MemoryRequestType.KREAD, MemoryRequestType.KREAD],
        )

        self.assertEqual(metrics.iops, 12345.0)
        self.assertEqual(engine.get_engine_metrics().iops, 12345.0)


if __name__ == "__main__":
    unittest.main()
