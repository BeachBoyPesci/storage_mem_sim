"""Memory Engine — core module for memory request lifecycle management.

MemoryEngine models one physical media instance. It handles address
allocation, request construction, and delegates performance simulation
to the configured MediaSystem backend.

Sync mode (``issue_request()``) provides batch throughput estimates.
Event mode (``submit()``) drives bandwidth competition directly on the
engine, returning per-request predictions computed via cascading
completion.
"""

import math
import logging
from typing import Dict, List, Tuple

from .memory_type import MemoryRequestType
from .memory_config import MemoryEngineConfig
from .memory_request import MemoryRequest
from .memory_metrics import MemoryMetrics, MemoryEngineMetrics

logger = logging.getLogger(__name__)

_DEFAULT_COMPLETION_EPSILON_BYTES = 1e-6


from dataclasses import dataclass


@dataclass
class _ActiveRequest:
    """Mutable state of one in-flight request in the engine."""

    request_id: str
    source_id: str
    mem_engine_id: int
    arrival_time: float
    remaining_bytes: float
    size_bytes: int
    allocated_bandwidth: float = 0.0


class MemoryEngine:
    """Single physical media instance.

    Sync mode: ``issue_request()`` delegates to the configured
    MediaSystem backend for batch throughput simulation.

    Event mode: ``submit()`` holds bandwidth competition state directly
    (active requests, allocated bandwidth, remaining bytes).  Each call
    advances time, collects completed requests, reallocates bandwidth,
    and returns full predictions for all active requests via cascading
    completion.
    """

    def __init__(self, mem_config: MemoryEngineConfig):
        """Create the engine and its MediaSystem backend.

        Uses MediaSystemFactory to instantiate the backend from
        *mem_config.media_config* and derives the address granularity
        from the backend (e.g. Ramulator ``_tx_bytes``, otherwise 64 B).
        """
        self.mem_config = mem_config
        self.global_addr: int = 0
        self.engine_metrics = MemoryEngineMetrics()

        self.instance_id: int = 0
        self.global_base: int = 0
        # Event-driven state.
        self._active_requests: Dict[str, "_ActiveRequest"] = {}
        self._last_update_time: float = 0.0

        if mem_config.media_config is None:
            raise ValueError("MemoryEngineConfig.media_config is required")
        from .media.media_system_factory import MediaSystemFactory
        self.media_system = MediaSystemFactory.create(mem_config.media_config)

        tx = getattr(self.media_system, '_tx_bytes', None)
        self.mem_config.granularity = tx if tx else 64

        logger.info(
            "MemoryEngine init: mem_type=%s granularity=%d capacity=%dGB",
            mem_config.memory_type.value, self.mem_config.granularity,
            mem_config.total_capacity // (1024 ** 3))

    # ------------------------------------------------------------------
    # Instance metadata
    # ------------------------------------------------------------------

    @property
    def capacity_bytes(self) -> int:
        return self.mem_config.total_capacity

    @property
    def remaining_capacity_bytes(self) -> int:
        return max(0, self.capacity_bytes - self.global_addr)

    # ------------------------------------------------------------------
    # Address allocation
    # ------------------------------------------------------------------

    def align_up(self, size: int) -> int:
        """Align *size* up to the engine's address granularity.

        Returns:
            Aligned size (ceiling to granularity boundary).
        """
        step = self.mem_config.granularity
        return math.ceil(size / step) * step

    def get_tensor_addr(self, size: int) -> int:
        """Allocate an aligned address for a tensor.

        The local address counter is advanced by the aligned size.
        Raises OverflowError if the allocation would exceed capacity.

        Returns:
            The starting local address for this tensor.
        """
        aligned_size = self.align_up(size)
        tensor_addr = self.global_addr
        self.global_addr += aligned_size
        if self.mem_config.per_dp_capacity > 0 and self.global_addr > self.mem_config.per_dp_capacity:
            raise OverflowError(
                f"Address overflow: global_addr {self.global_addr} exceeds "
                f"per_dp_capacity {self.mem_config.per_dp_capacity}"
            )
        return tensor_addr

    def reset_addr(self):
        """Reset the local address counter to zero."""
        self.global_addr = 0

    # ------------------------------------------------------------------
    # Sync batch path
    # ------------------------------------------------------------------

    # TODO: issue_request will be deprecated or substantially reworked
    # in a future phase — the sync batch path overlaps with the event
    # path conceptually and should be unified.
    def issue_request(
        self,
        addr: List[int],
        size: List[int],
        req_type: List[MemoryRequestType],
    ) -> MemoryMetrics:
        """Execute a synchronous batch of requests.

        All three lists must have the same length.  Each request targets
        this single instance (no DP replication, no cross-instance
        routing).  Returns per-request and cumulative metrics.
        """
        if self.media_system is None:
            raise RuntimeError("No media_system configured.")
        n = len(addr)
        if len(size) != n or len(req_type) != n:
            raise ValueError("addr, size, req_type must have the same length")
        for i in range(n):
            if addr[i] < 0:
                raise ValueError(f"addr[{i}] must be >= 0")
            if size[i] <= 0:
                raise ValueError(f"size[{i}] must be > 0")
        if n == 0:
            return MemoryMetrics()
        mem_reqs = [
            MemoryRequest(addr[i], size[i], req_type[i], config=self.mem_config)
            for i in range(n)
        ]
        total_media_metrics = self.media_system.handler_mem_request(mem_reqs)
        simulated_bytes = sum(req.size for req in mem_reqs)
        mem_metrics = MemoryMetrics(
            cycles=total_media_metrics.cycles,
            total_time=total_media_metrics.time,
            memory_reqs_num=n,
            global_memory_reqs_num=n,
            bandwidth=total_media_metrics.bandwidth,
            iops=total_media_metrics.iops,
            iops_read=total_media_metrics.iops_read,
            iops_write=total_media_metrics.iops_write,
        )
        self.engine_metrics.update(mem_metrics, simulated_bytes)
        return mem_metrics

    def get_engine_metrics(self) -> MemoryEngineMetrics:
        """Return the cumulative sync-path metrics."""
        return self.engine_metrics

    def reset_engine_metrics(self):
        """Reset cumulative sync-path metrics."""
        self.engine_metrics = MemoryEngineMetrics()

    # ------------------------------------------------------------------
    # Event-driven path
    # ------------------------------------------------------------------

    def submit(
        self,
        request: "MemoryRequest",
        local_addr: int,
        now: float,
    ) -> List[Tuple[str, float, "MemoryRequestMetrics"]]:
        """Submit one access; return earliest-finishing predictions.

        If *request.is_finish* is True, this is a FINISH callback:
        the completed request is removed and bandwidth is reallocated.
        Otherwise it is an ARRIVAL: a new request is added.
        """
        self._advance(now)
        self._pop_finished()

        if request.is_finish:
            # Only the earliest-finishing request(s) hold a FINISH event.
            # A sibling callback (same time) may have already popped this
            # request via _pop_finished — that is expected and harmless.
            if request.request_id in self._active_requests:
                self._active_requests.pop(request.request_id)
        else:
            if request.request_id in self._active_requests:
                raise ValueError(
                    f"request_id {request.request_id!r} is already active"
                )
            self._active_requests[request.request_id] = _ActiveRequest(
                request_id=request.request_id,
                source_id=request.source_id,
                mem_engine_id=request.mem_engine_id or self.instance_id,
                arrival_time=now,
                remaining_bytes=float(request.size),
                size_bytes=request.size,
            )

        self._reallocate()

        predictions = self._predict_earliest(now)
        return self._build_results(predictions)

    # ------------------------------------------------------------------
    # Internal helpers (called in submit order)
    # ------------------------------------------------------------------

    def _advance(self, now: float) -> None:
        """Advance the engine clock to *now*, consuming bandwidth.

        For each active request, subtracts ``allocated_bw × delta``
        from ``remaining_bytes``.  Raises ValueError if *now* is earlier
        than the last update.
        """
        if now < self._last_update_time:
            raise ValueError(
                f"now must not go backwards: "
                f"last={self._last_update_time}, now={now}"
            )
        delta = now - self._last_update_time
        if delta > 0 and self._active_requests:
            for req in self._active_requests.values():
                req.remaining_bytes -= req.allocated_bandwidth * delta
                req.remaining_bytes = max(0.0, req.remaining_bytes)
        self._last_update_time = now

    def _pop_finished(self) -> None:
        """Pop finished requests from the active set."""
        for rid, req in list(self._active_requests.items()):
            if req.remaining_bytes <= _DEFAULT_COMPLETION_EPSILON_BYTES:
                self._active_requests.pop(rid)

    def _reallocate(self) -> None:
        """Equal split of effective bandwidth among active requests."""
        if not self._active_requests:
            return
        bw = self.media_system._bandwidth_bytes_per_sec
        share = bw / len(self._active_requests)
        for req in self._active_requests.values():
            req.allocated_bandwidth = share

    def _predict_earliest(self, now: float) -> List[Tuple[str, float]]:
        """Return ``[(rid, ft), ...]`` for the earliest-finishing request(s).

        Pure computation — no side effects, no metrics allocation.
        """
        if not self._active_requests:
            return []

        all_ft: List[Tuple[str, float]] = []
        for rid, req in self._active_requests.items():
            if req.allocated_bandwidth > 0:
                ft = now + req.remaining_bytes / req.allocated_bandwidth
            else:
                ft = float("inf")
            all_ft.append((rid, ft))

        min_ft = min(ft for _, ft in all_ft)
        return [(rid, ft) for rid, ft in all_ft if ft == min_ft]

    def _build_results(
        self, predictions: List[Tuple[str, float]],
    ) -> List[Tuple[str, float, "MemoryRequestMetrics"]]:
        """Wrap ``[(rid, ft), ...]`` with MemoryRequestMetrics."""
        from .memory_pool.request_metrics import MemoryRequestMetrics

        effective_bw = self.media_system._bandwidth_bytes_per_sec
        result: List[Tuple[str, float, MemoryRequestMetrics]] = []
        for rid, ft in predictions:
            req = self._active_requests[rid]
            latency = ft - req.arrival_time
            standalone = req.size_bytes / effective_bw if effective_bw > 0 else float("inf")
            m = MemoryRequestMetrics(
                request_id=rid,
                source_id=req.source_id,
                mem_engine_id=req.mem_engine_id,
                arrival_time=req.arrival_time,
                finish_time=ft,
                size=req.size_bytes,
                latency=latency,
                standalone_time=standalone,
                contention_delay=latency - standalone,
                average_bandwidth=(
                    req.size_bytes / latency if latency > 0 else 0.0
                ),
            )
            result.append((rid, ft, m))
        return result
