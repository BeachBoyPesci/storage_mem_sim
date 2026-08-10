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
from typing import Dict, List, Optional, Tuple

from .memory_type import MemoryRequestType
from .memory_config import MemoryEngineConfig
from .memory_object import MemoryObject
from .memory_request import MemoryRequest
from .memory_metrics import MemoryMetrics, MemoryEngineMetrics

logger = logging.getLogger(__name__)

_DEFAULT_COMPLETION_EPSILON_BYTES = 1e-6


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
        self._runtime_mode: Optional[str] = None

        # Event-driven state.
        self._active_requests: Dict[str, "ActiveMemoryRequest"] = {}
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

    def _enter_runtime_mode(self, mode: str) -> None:
        if self._runtime_mode is None:
            self._runtime_mode = mode
            return
        if self._runtime_mode != mode:
            raise RuntimeError(
                f"cannot use {mode!r} runtime mode: engine locked to "
                f"{self._runtime_mode!r}"
            )

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

    def create_request(
        self, addr: int, size: int, req_type: MemoryRequestType,
    ) -> MemoryRequest:
        """Wrap *addr*, *size*, *req_type* into a MemoryRequest.

        Constructs a MemoryObject then wraps it.  The object's
        ``media_req_num`` is an estimate based on engine granularity;
        the true count comes from the backend.
        """
        memory_object = MemoryObject(addr, size, req_type, self.mem_config)
        return MemoryRequest(memory_object=memory_object)

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
        self._enter_runtime_mode("sync")
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
            self.create_request(addr[i], size[i], req_type[i])
            for i in range(n)
        ]
        total_media_metrics = self.media_system.handler_mem_request(mem_reqs)
        simulated_bytes = sum(req.memory_object.size for req in mem_reqs)
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
        access: "MemoryAccess",
        local_addr: int,
        now: float,
    ) -> List[Tuple[str, float, "MemoryRequestMetrics"]]:
        """Submit one access; return [(rid, finish_time, metrics), ...].

        Metrics are computed at prediction time and bound to each
        returned entry.  The Simulator creates FINISH events with
        these metrics already attached.
        """
        from .memory_pool.memory_access import ActiveMemoryRequest, MemoryRequestMetrics

        self._enter_runtime_mode("event")
        self._advance(now)
        self._collect_completed()

        if access.request_id in self._active_requests:
            raise ValueError(
                f"request_id {access.request_id!r} is already active"
            )
        self._active_requests[access.request_id] = ActiveMemoryRequest(
            access=access,
            local_addr=local_addr,
            mem_engine_id=self.instance_id,
            arrival_time=now,
            remaining_bytes=float(access.size_bytes),
        )

        self._reallocate()

        # Build predictions via cascading completion simulation.
        effective_bw = self.media_system.effective_bandwidth
        cascaded = self._cascade_predictions(now, self._active_requests, effective_bw)

        def _make_metrics(req, finish_time):
            latency = finish_time - req.arrival_time
            standalone = req.access.size_bytes / effective_bw if effective_bw > 0 else float("inf")
            return MemoryRequestMetrics(
                request_id=req.access.request_id,
                source_id=req.access.source_id,
                mem_engine_id=req.mem_engine_id,
                arrival_time=req.arrival_time,
                finish_time=finish_time,
                size_bytes=req.access.size_bytes,
                latency=latency,
                standalone_time=standalone,
                contention_delay=latency - standalone,
                average_bandwidth=(
                    req.access.size_bytes / latency if latency > 0 else 0.0
                ),
            )

        result: List[Tuple[str, float, MemoryRequestMetrics]] = []
        for rid, req in self._active_requests.items():
            ft = cascaded[rid]
            m = _make_metrics(req, ft)
            result.append((rid, ft, m))

        return result

    # ------------------------------------------------------------------
    # Internal helpers
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
                transferred = req.allocated_bandwidth * delta
                req.transferred_bytes += transferred
                req.remaining_bytes = max(
                    0.0, req.remaining_bytes - transferred
                )
        self._last_update_time = now

    def _collect_completed(self) -> None:
        """Pop finished requests from the active set."""
        completed_ids = [
            rid for rid, req in self._active_requests.items()
            if req.remaining_bytes <= _DEFAULT_COMPLETION_EPSILON_BYTES
        ]
        for rid in completed_ids:
            self._active_requests.pop(rid)

    def _reallocate(self) -> None:
        """Equal split of effective bandwidth among active requests."""
        if not self._active_requests:
            return
        bw = self.media_system.effective_bandwidth
        share = bw / len(self._active_requests)
        for req in self._active_requests.values():
            req.allocated_bandwidth = share

    def _cascade_predictions(
        self,
        now: float,
        active: dict,
        effective_bw: float,
    ) -> dict:
        """Return ``{request_id: finish_time}`` via cascading completion.

        Simulates each request finishing in order of earliest projected
        end, reallocating the freed bandwidth to the survivors at each
        step.  Pure computation — no side effects.
        """
        if effective_bw <= 0:
            return {rid: float("inf") for rid in active}

        # Working copies: each entry is [rid, arrival, remaining].
        pending = [
            [rid, req.arrival_time, req.remaining_bytes]
            for rid, req in active.items()
        ]
        n = len(pending)
        share = effective_bw / n
        sim_time = float(now)
        result: dict[str, float] = {}

        while pending:
            # Find the request that finishes earliest.
            earliest_idx = 0
            earliest_delta = pending[0][2] / share
            for i, (_, _, rem) in enumerate(pending):
                delta = rem / share
                if delta < earliest_delta:
                    earliest_delta = delta
                    earliest_idx = i

            sim_time += earliest_delta
            for p in pending:
                p[2] -= share * earliest_delta

            rid = pending.pop(earliest_idx)[0]
            result[rid] = sim_time

            if pending:
                share = effective_bw / len(pending)

        return result
