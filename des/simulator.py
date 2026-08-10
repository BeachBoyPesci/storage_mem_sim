"""SimpleSimulator — thin DES adapter that drives a MemoryPool."""

import heapq
import logging
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from ..memory_pool import MemoryRequestMetrics
from ..memory_type import MemoryRequestType
from .result import SimulationResult

if TYPE_CHECKING:
    from ..memory_pool import MemoryPool

logger = logging.getLogger(__name__)


class SimpleSimulator:
    """Discrete-event loop driving the pool's event API.

    Does not know about individual engines. Receives incremental
    predictions from pool.submit() and manages the event heap.
    """

    def __init__(self, pool: "MemoryPool"):
        from ..memory_pool.event import Event

        self._pool = pool
        self._heap: List[Tuple[float, int, "Event"]] = []
        self._seq = 0
        self._scheduled_count = 0
        self._request_metrics: List[MemoryRequestMetrics] = []

    def schedule_arrival(
        self,
        time: float,
        source_id: str,
        size_bytes: int,
        *,
        req_type: Optional[MemoryRequestType] = None,
        addr: int,
        engine_id: Optional[int] = None,
    ) -> str:
        """Schedule an arrival event. *addr* must be pool-global.

        Args:
            time: Arrival time in seconds.
            source_id: Access origin identifier.
            size_bytes: Request size in bytes.
            req_type: Request type (default KREAD).
            addr: Pool-global byte address.
            engine_id: Optional target engine instance (None = resolve by address).
        """
        from ..memory_pool.event import Event, EventKind

        if req_type is None:
            req_type = MemoryRequestType.KREAD

        request_id = f"req:{source_id}:{self._scheduled_count}"
        self._scheduled_count += 1

        self._seq += 1
        ev = Event(
            time=time, seq=0, kind=EventKind.ARRIVAL,
            source_id=source_id, request_id=request_id,
            mem_engine_id=engine_id,
            addr=addr, size_bytes=size_bytes,
            req_type=req_type,
        )
        heapq.heappush(self._heap, (ev.time, self._seq, ev))
        return request_id

    def run(self) -> SimulationResult:
        """Process the event queue until empty."""
        from ..memory_pool.event import EventKind

        while self._heap:
            event = heapq.heappop(self._heap)[2]
            if event.kind is EventKind.ARRIVAL:
                self._handle_arrival(event)
            else:
                self._handle_finish(event)

        makespan = 0.0
        if self._request_metrics:
            makespan = max(m.finish_time for m in self._request_metrics)

        per_source: Dict[str, list] = {}
        for m in self._request_metrics:
            per_source.setdefault(m.source_id, []).append(m)

        return SimulationResult(
            request_metrics=list(self._request_metrics),
            per_source={
                s: {
                    "avg_latency": sum(m.latency for m in ms) / len(ms),
                    "avg_contention_delay": sum(m.contention_delay for m in ms) / len(ms),
                    "total_bytes": sum(m.size_bytes for m in ms),
                    "count": len(ms),
                }
                for s, ms in per_source.items()
            },
            makespan=makespan,
            scheduled_finish_events=self._scheduled_count,
            stale_finish_events=0,
        )

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    def _handle_arrival(self, event) -> None:
        entries = self._pool.submit(event)
        # entries: [(rid, finish_time, metrics), ...]
        self._refresh_finish_events(entries)

    def _handle_finish(self, event) -> None:
        if event.metrics is not None:
            self._request_metrics.append(event.metrics)

    def _refresh_finish_events(self, entries: list) -> None:
        from ..memory_pool.event import Event, EventKind

        for request_id, finish_time, metrics in entries:
            self._heap = [
                e for e in self._heap
                if not (e[2].kind is EventKind.FINISH
                        and e[2].request_id == request_id)
            ]
            heapq.heapify(self._heap)

            self._seq += 1
            ev = Event(
                time=finish_time, seq=0, kind=EventKind.FINISH,
                source_id="", request_id=request_id,
                mem_engine_id=0,
                addr=0, size_bytes=0, req_type=MemoryRequestType.KREAD,
                metrics=metrics,
            )
            heapq.heappush(self._heap, (ev.time, self._seq, ev))
