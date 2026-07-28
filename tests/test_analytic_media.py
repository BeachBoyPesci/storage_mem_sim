"""Tests for AnalyticMediaSystem — roofline estimation backend."""

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
    MediaConfig,
    MediaSystemBackend,
    AnalyticMediaSystem,
    MediaMetrics,
)


class TestAnalyticMediaSystem(unittest.TestCase):
    """Test the analytic/roofline backend."""

    def setUp(self):
        self.media_config = MediaConfig(
            media_type=MediaSystemBackend.ANALYTIC,
            bandwidth=100.0,  # 100 GB/s
        )
        self.system = AnalyticMediaSystem(self.media_config)
        self.mem_config = MemoryEngineConfig(media_config=MediaConfig(media_type=MediaSystemBackend.ANALYTIC,bandwidth=100.0,capacity=1.0))

    def _make_memory_request(self, addr, size, req_type):
        """Helper: create a MemoryRequest for testing."""
        obj = MemoryObject(addr, size, req_type, self.mem_config)
        return MemoryRequest(memory_object=obj)

    def test_single_read(self):
        """Single read request produces valid metrics."""
        req = self._make_memory_request(0, 64, MemoryRequestType.KREAD)
        metrics = self.system.handler_mem_request([req])

        self.assertIsInstance(metrics, MediaMetrics)
        self.assertEqual(metrics.num_read_requests, 1)
        self.assertEqual(metrics.num_write_requests, 0)
        self.assertEqual(metrics.num_media_reqs, 1)
        self.assertGreater(metrics.time, 0)

    def test_single_write(self):
        """Single write request counts correctly."""
        req = self._make_memory_request(0, 64, MemoryRequestType.KWRITE)
        metrics = self.system.handler_mem_request([req])

        self.assertEqual(metrics.num_read_requests, 0)
        self.assertEqual(metrics.num_write_requests, 1)
        self.assertGreater(metrics.time, 0)

    def test_time_scales_with_size(self):
        """Larger requests should take proportionally more time."""
        req_small = self._make_memory_request(0, 64, MemoryRequestType.KREAD)
        req_large = self._make_memory_request(0, 128, MemoryRequestType.KREAD)

        m_small = self.system.handler_mem_request([req_small])
        m_large = self.system.handler_mem_request([req_large])

        # Large (128B) should take about 2x small (64B)
        ratio = m_large.time / m_small.time
        self.assertAlmostEqual(ratio, 2.0, places=1)

    def test_empty_request_list(self):
        """Empty request list returns zero metrics."""
        metrics = self.system.handler_mem_request([])
        self.assertEqual(metrics.time, 0.0)
        self.assertEqual(metrics.num_media_reqs, 0)

    def test_multiple_requests(self):
        """Multiple requests accumulate time correctly."""
        reqs = [
            self._make_memory_request(0, 64, MemoryRequestType.KREAD),
            self._make_memory_request(64, 128, MemoryRequestType.KWRITE),
        ]
        metrics = self.system.handler_mem_request(reqs)
        self.assertEqual(metrics.num_media_reqs, 2)

    def test_system_metrics_accumulate(self):
        """System metrics should accumulate across handler_mem_request calls."""
        req = self._make_memory_request(0, 64, MemoryRequestType.KREAD)

        self.system.handler_mem_request([req])
        self.system.handler_mem_request([req])

        sys_metrics = self.system.get_system_metrics()
        self.assertEqual(sys_metrics.num_read_requests, 2)
        self.assertEqual(len(sys_metrics.media_metrics_list), 2)

    def test_reset_system_metrics(self):
        """Resetting clears accumulated system metrics."""
        req = self._make_memory_request(0, 64, MemoryRequestType.KREAD)
        self.system.handler_mem_request([req])
        self.system.reset_system_metrics()

        sys_metrics = self.system.get_system_metrics()
        self.assertEqual(sys_metrics.num_read_requests, 0)
        self.assertEqual(len(sys_metrics.media_metrics_list), 0)

    def test_bandwidth_config(self):
        """Higher bandwidth → lower access time."""
        req = self._make_memory_request(0, 100 * 1024**3, MemoryRequestType.KREAD)

        # 100 GB/s
        sys_100 = AnalyticMediaSystem(
            MediaConfig(media_type=MediaSystemBackend.ANALYTIC, bandwidth=100.0)
        )
        m_100 = sys_100.handler_mem_request([req])

        # 200 GB/s
        sys_200 = AnalyticMediaSystem(
            MediaConfig(media_type=MediaSystemBackend.ANALYTIC, bandwidth=200.0)
        )
        m_200 = sys_200.handler_mem_request([req])

        # Double bandwidth → half time
        self.assertAlmostEqual(m_100.time, 2 * m_200.time, places=3)

    def test_analytic_does_not_report_iops(self):
        """Analytic models bandwidth only; device IOPS is not applicable."""
        reqs = [
            self._make_memory_request(0, 64, MemoryRequestType.KREAD),
            self._make_memory_request(64, 64, MemoryRequestType.KWRITE),
        ]
        metrics = self.system.handler_mem_request(reqs)
        self.assertEqual(metrics.bandwidth, 100.0 * 1024**3)
        self.assertIsNone(metrics.iops)
        self.assertIsNone(metrics.iops_read)
        self.assertIsNone(metrics.iops_write)

    def test_media_metrics_add_time_weights_rates(self):
        """Adding batches preserves rate fields instead of dropping them."""
        left = MediaMetrics(
            time=1.0, bandwidth=100.0, iops=10.0,
            iops_read=8.0, iops_write=2.0,
        )
        right = MediaMetrics(
            time=3.0, bandwidth=300.0, iops=30.0,
            iops_read=12.0, iops_write=18.0,
        )
        combined = left + right
        self.assertEqual(combined.time, 4.0)
        self.assertEqual(combined.bandwidth, 250.0)
        self.assertEqual(combined.iops, 25.0)
        self.assertEqual(combined.iops_read, 11.0)
        self.assertEqual(combined.iops_write, 14.0)


if __name__ == "__main__":
    unittest.main()
