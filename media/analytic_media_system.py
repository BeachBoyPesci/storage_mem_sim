"""Analytic media system — pure roofline estimation.

Computes access time as total_data_size / bandwidth. No cycle-level
simulation. Suitable for rapid prototyping and early-stage analysis.
"""

import logging
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory_request import MemoryRequest

from .base_media import BaseMediaSystem
from .media_config import MediaConfig
from .media_metrics import MediaMetrics
from ..memory_type import MemoryRequestType

logger = logging.getLogger(__name__)


class AnalyticMediaSystem(BaseMediaSystem):
    """Pure analytical backend using size/bandwidth estimation.

    Computes total access time as the sum of (size / bandwidth) for each
    request. This is a zero-fidelity model — it ignores all queuing,
    contention, scheduling, and device-level timing effects.

    Usage:
        config = MediaConfig(
            media_type=MediaSystemBackend.ANALYTIC,
            bandwidth=100.0,      # GB/s
        )
        sys = AnalyticMediaSystem(config)
        metrics = sys.handler_mem_request(mem_req_list)
    """

    def __init__(self, config: MediaConfig):
        super().__init__(config)
        if config.bandwidth <= 0:
            raise ValueError(
                f"bandwidth must be > 0 for Analytic backend, got {config.bandwidth}"
            )
        # Convert bandwidth from GB/s (GiB/s semantics) to B/s.
        # transport_bandwidth == 0 means no transport bottleneck.
        self._media_bw_bps = config.bandwidth * (1024 ** 3)
        _tb = config.transport_bandwidth
        self._transport_bw_bps = _tb * (1024 ** 3) if _tb > 0 else None
        logger.info("Analytic backend ready: bandwidth=%.1f GB/s", config.bandwidth)

    # ------------------------------------------------------------------
    # Read-only bandwidth properties (all in B/s)
    # ------------------------------------------------------------------

    @property
    def media_bandwidth(self) -> float:
        """Media peak bandwidth in B/s."""
        return self._media_bw_bps

    @property
    def transport_bandwidth(self) -> float | None:
        """Configured transport bandwidth in B/s, or None if not set."""
        return self._transport_bw_bps

    @property
    def effective_bandwidth(self) -> float:
        """Effective peak bandwidth in B/s: min(media, transport)."""
        if self._transport_bw_bps is None:
            return self._media_bw_bps
        return min(self._media_bw_bps, self._transport_bw_bps)

    def handler_mem_request(
        self, mem_req_list: List["MemoryRequest"]
    ) -> MediaMetrics:
        """Estimate total time as total_bytes / bandwidth.

        Args:
            mem_req_list: List of MemoryRequest objects.

        Returns:
            MediaMetrics with time and request counts populated.
        """
        total_bytes = 0
        num_read = 0
        num_write = 0

        for mem_req in mem_req_list:
            obj = mem_req.memory_object
            total_bytes += obj.size

            if obj.req_type == MemoryRequestType.KREAD:
                num_read += 1
            elif obj.req_type == MemoryRequestType.KWRITE:
                num_write += 1

        total_time = total_bytes / self.effective_bandwidth if self.effective_bandwidth > 0 else 0.0
        metrics = MediaMetrics(
            num_read_requests=num_read,
            num_write_requests=num_write,
            num_other_requests=0,
            cycles=0,
            num_media_reqs=len(mem_req_list),
            time=total_time,
            bandwidth=self._media_bw_bps if total_time > 0 else 0.0,
        )

        self.system_metrics.update_from_media(metrics)
        logger.debug(
            "Analytic handler: time=%.4fus reads=%d writes=%d "
            "total_bytes=%d",
            total_time * 1e6, num_read, num_write,
            total_bytes)
        return metrics
