"""SimpleSimulator — thin DES adapter that drives a MemoryPool."""

import heapq
import logging
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from ..memory_pool import MemoryAccess, MemoryRequestMetrics
from ..memory_type import MemoryRequestType
from .event import Event
from .result import SimulationResult

if TYPE_CHECKING:
    from ..memory_pool import MemoryPool

logger = logging.getLogger(__name__)


class SimpleSimulator:
    """Discrete-event loop driving the pool's event API.

    Does not know about individual engines.  Events carry callbacks
    that call pool.submit() — the simulator just pops and invokes.
    """

    def __init__(self, pool: "MemoryPool"):
        self._memory_pool = pool
        self._events: List[Tuple[float, int, "Event"]] = []
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
        """Schedule an arrival event. *addr* must be pool-global."""
        if req_type is None:
            req_type = MemoryRequestType.KREAD

        request_id = f"req:{source_id}:{self._scheduled_count}"
        self._scheduled_count += 1
        pool = self._memory_pool

        def _on_arrival() -> None:
            access = MemoryAccess(
                request_id=request_id,
                source_id=source_id,
                addr=addr,
                size_bytes=size_bytes,
                req_type=req_type,
                mem_engine_id=engine_id,
                is_finish=False,
            )
            self._refresh_finish_events(
                pool.submit(access, now=time),
            )

        self._push_event(Event(time=time, callback=_on_arrival,
                               request_id=request_id))
        return request_id

    def run(self) -> SimulationResult:
        """Process the event queue until empty."""
        while self._events:
            event = heapq.heappop(self._events)[2]
            event.callback()

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
    # Internal helpers
    # ------------------------------------------------------------------

    def _push_event(self, event: Event) -> None:
        self._seq += 1
        object.__setattr__(event, 'seq', self._seq)
        heapq.heappush(self._events, (event.time, event.seq, event))

    def _refresh_finish_events(self, entries: list) -> None:
        """Replace stale FINISH events and create new ones."""
        pool = self._memory_pool

        for request_id, finish_time, metrics in entries:
            # Remove old FINISH: scan, pop, restore heap.
            for i in range(len(self._events)):
                ev = self._events[i][2]
                if ev.metrics is not None and ev.request_id == request_id:
                    self._events[i] = self._events[-1]
                    self._events.pop()
                    if i < len(self._events):
                        heapq._siftup(self._events, i)
                        heapq._siftdown(self._events, 0, i)
                    break

            def _on_finish() -> None:
                if metrics is not None:
                    self._request_metrics.append(metrics)
                access = MemoryAccess(
                    request_id=request_id,
                    source_id=metrics.source_id,
                    addr=0,
                    size_bytes=metrics.size_bytes,
                    req_type=MemoryRequestType.KREAD,
                    mem_engine_id=metrics.mem_engine_id,
                    is_finish=True,
                )
                self._refresh_finish_events(
                    pool.submit(access, now=finish_time),
                )

            self._push_event(Event(
                time=finish_time, callback=_on_finish,
                request_id=request_id, metrics=metrics,
            ))
