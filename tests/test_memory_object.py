"""Tests for MemoryRequest creation and decomposition logic."""

import unittest

from ..memory_type import MemoryType, MemoryRequestType
from ..memory_config import MemoryEngineConfig
from ..memory_request import MemoryRequest
from ..media import MediaConfig, MediaSystemBackend


class TestMemoryRequestMediaReqNum(unittest.TestCase):
    """Test MemoryRequest media_req_num computation."""

    def setUp(self):
        self.config = MemoryEngineConfig(
            memory_type=MemoryType.HBM,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC, capacity=1.0),
        )
        self.config.granularity = 64

    def test_exact_granularity_size(self):
        """Size exactly one granularity unit → 1 media request."""
        req = MemoryRequest(0, 64, MemoryRequestType.KREAD, config=self.config)
        self.assertEqual(req.media_req_num, 1)
        self.assertEqual(req.addr, 0)
        self.assertEqual(req.size, 64)

    def test_partial_granularity_size(self):
        """Size smaller than granularity → still 1 media request (ceiling)."""
        req = MemoryRequest(0, 32, MemoryRequestType.KWRITE, config=self.config)
        self.assertEqual(req.media_req_num, 1)

    def test_multi_granularity_size(self):
        """Size spanning multiple granularity units."""
        req = MemoryRequest(128, 256, MemoryRequestType.KREAD, config=self.config)
        self.assertEqual(req.media_req_num, 4)

    def test_create_request(self):
        """MemoryRequest starts with empty media request list."""
        req = MemoryRequest(4096, 128, MemoryRequestType.KWRITE, config=self.config)
        self.assertEqual(req.addr, 4096)
        self.assertEqual(req.size, 128)
        self.assertEqual(req.media_request_list, [])


if __name__ == "__main__":
    unittest.main()
