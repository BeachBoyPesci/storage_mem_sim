"""Memory Engine — core module for memory request lifecycle management.

MemoryEngine models one physical media instance. It handles address
allocation, request construction, and delegates performance simulation
to the configured MediaSystem backend.

Sync mode (``issue_request()``) provides batch throughput estimates.
Event mode (``submit()``) drives bandwidth competition directly on the
engine: every state change (an ARRIVAL or a FINISH) returns a **full
snapshot** — predictions for all active requests — so the simulator can
refresh every FINISH event.  An event therefore always fires at the
request's true completion; stale events are skipped by the simulator.
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
class MemoryEngine:
    """Single physical media instance.

    Sync mode: ``issue_request()`` delegates to the configured
    MediaSystem backend for batch throughput simulation.

    Event mode: ``submit()`` holds bandwidth competition state directly
    (active requests, allocated bandwidth, remaining bytes).  Equal-split
    reallocation moves the finish time of *every* active request on every
    call, so ``submit()``/``finish()`` return predictions for **all** active
    requests and the simulator refreshes every FINISH event from them.
    Consequently a FINISH event always fires exactly at the request's true
    completion under the latest schedule; events that were superseded are
    skipped as stale by the simulator.  No event ever needs to be refused.
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
        self._active_requests: Dict[str, "MemoryRequest"] = {}
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
        now: float,
    ) -> List["MemoryRequest"]:
        """Submit an ARRIVAL; return predictions for all active requests.

        Advances the engine clock, collects requests that have already
        exhausted their remaining bytes, inserts *request*, reallocates
        bandwidth equally among all active requests, and returns a **full
        snapshot** — every active request with its predicted ``metrics``
        (``finish_time`` under the current equal split), in insertion order.

        The snapshot is the report of everything that changed: an equal-split
        reallocation moves the finish time of *every* active request, so the
        caller (the simulator) refreshes each request's FINISH event from
        this list.  That keeps every scheduled event equal to the latest
        prediction, which guarantees a FINISH event fires exactly at the
        request's true completion — the simulator never needs to second-guess
        a completion.
        """
        self._advance(now)
        self._pop_finished()

        if request.request_id in self._active_requests:
            raise ValueError(
                f"request_id {request.request_id!r} is already active"
            )
        request.arrival_time = now
        request.remaining_bytes = float(request.size)
        request.allocated_bandwidth = 0.0
        if request.mem_engine_id is None:
            request.mem_engine_id = self.instance_id
        self._active_requests[request.request_id] = request

        self._reallocate()
        return self._build_results(self._predict_all(now))

    def finish(
        self, rid: str, now: float,
    ) -> List["MemoryRequest"]:
        """Handle a FINISH event; return predictions for all active requests.

        The fired event is by construction the request's latest prediction
        (the simulator refreshes every event from every engine snapshot), so
        its remaining bytes are exhausted at *now* (within float noise of the
        epsilon) and *rid* is popped.  After the pop the engine reallocates
        and returns the full snapshot of the surviving requests — possibly
        empty when the engine is idle.

        Two defensive notes:

        - If *rid* is absent (already collected by ``_pop_finished`` or a
          same-time sibling event), the engine simply returns the snapshot of
          the remaining requests; the fired event's metrics were captured at
          scheduling time and remain final.
        - If *rid* is present but its ``remaining_bytes`` is still above the
          epsilon (only reachable through float round-off on very large
          requests, never through a genuinely stale event), it is left
          active: it appears in the returned snapshot with a corrected
          ``finish_time`` (≈ *now*), the simulator reschedules it, and the
          next event completes it.  There is no refusal protocol — the
          snapshot carries the correction naturally.
        """
        self._advance(now)
        self._pop_finished()
        req = self._active_requests.get(rid)
        if req is not None and (
            req.remaining_bytes <= _DEFAULT_COMPLETION_EPSILON_BYTES
        ):
            self._active_requests.pop(rid)
        self._reallocate()
        return self._build_results(self._predict_all(now))

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
        """Pop requests whose remaining bytes are exhausted (within epsilon).

        Called between ``_advance`` and ``_reallocate`` so that the equal
        split and the prediction snapshot only cover requests that are still
        genuinely in flight.  Safe because every state change also returns a
        full snapshot: a request collected here still holds a scheduled
        FINISH event (it was part of the previous snapshot), so its metrics
        are recorded when that event fires, and later events for it hit the
        defensive path.
        """
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

    def _predict_all(self, now: float) -> List[Tuple[str, float]]:
        """Return ``(rid, ft)`` predictions for every active request.

        Pure computation — no side effects, no metrics allocation.  Fts are
        computed under the current equal split; they are provisional (a
        future completion or arrival changes everyone's share), which is why
        every state change returns this full snapshot so the caller can
        refresh all FINISH events.
        """
        result: List[Tuple[str, float]] = []
        for rid, req in self._active_requests.items():
            if req.allocated_bandwidth <= 0:
                # Invariant violation: every active request gets a positive
                # share from _reallocate() before a prediction.  A non-positive
                # share means the call order was broken (or the backend
                # bandwidth is not > 0) — fail loudly instead of silently
                # predicting a request that never completes.
                raise ValueError(
                    f"cannot predict request {rid!r}: allocated_bandwidth "
                    f"= {req.allocated_bandwidth} must be > 0; "
                    "_reallocate() must run before _predict_all()"
                )
            ft = now + req.remaining_bytes / req.allocated_bandwidth
            result.append((rid, ft))
        return result

    def _build_results(
        self, predictions: List[Tuple[str, float]],
    ) -> List["MemoryRequest"]:
        """Attach MemoryRequestMetrics to each prediction and return them.

        Metrics are computed at prediction time and bound to the request.
        They become final when the request's (refreshed) FINISH event fires;
        an event only fires at the latest prediction, so the metrics it
        captured are the final ones.
        """
        from .memory_pool.request_metrics import MemoryRequestMetrics

        effective_bw = self.media_system._bandwidth_bytes_per_sec
        result: List["MemoryRequest"] = []
        for rid, ft in predictions:
            req = self._active_requests[rid]
            latency = ft - req.arrival_time
            standalone = req.size / effective_bw if effective_bw > 0 else float("inf")
            req.metrics = MemoryRequestMetrics(
                request_id=rid,
                source_id=req.source_id,
                mem_engine_id=req.mem_engine_id,
                arrival_time=req.arrival_time,
                finish_time=ft,
                size=req.size,
                latency=latency,
                standalone_time=standalone,
                contention_delay=latency - standalone,
                average_bandwidth=(
                    req.size / latency if latency > 0 else 0.0
                ),
            )
            result.append(req)
        return result
