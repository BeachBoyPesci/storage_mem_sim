"""SimpleSimulator — thin DES adapter that drives a MemoryPool."""

import heapq
import logging
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from ..memory_request import MemoryRequest
from ..memory_pool import MemoryRequestMetrics
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
        self._stale_count = 0
        self._request_metrics: List[MemoryRequestMetrics] = []
        self._latest_finish: Dict[str, float] = {}

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
            # TODO: support dispatching multiple MemoryRequests from a
            # single arrival event (e.g. read + write, or multi-address
            # scatter/gather).  Currently one event → one request.
            request = MemoryRequest(
                addr, size_bytes, req_type,
                request_id=request_id,
                source_id=source_id,
                mem_engine_id=engine_id,
            )
            self._refresh_finish_events(
                pool.submit(request, now=time),
            )

        self._push_event(Event(time=time, callback=_on_arrival,
                               request_id=request_id))
        return request_id

    def run(self) -> SimulationResult:
        """Process the event queue until empty."""
        while self._events:
            event = heapq.heappop(self._events)[2]
            if (event.metrics is not None
                    and event.time != self._latest_finish.get(event.request_id)):
                self._stale_count += 1
                continue
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
                    "total_bytes": sum(m.size for m in ms),
                    "count": len(ms),
                }
                for s, ms in per_source.items()
            },
            makespan=makespan,
            scheduled_finish_events=self._scheduled_count,
            stale_finish_events=self._stale_count,
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

        for req in entries:
            m = req.metrics
            rid = m.request_id
            ft = m.finish_time
            # Record latest prediction — old FINISH events for this rid
            # are now stale and will be skipped on pop.
            self._latest_finish[rid] = ft

            def _on_finish(_m=m, _rid=rid, _ft=ft) -> None:
                if _m is not None:
                    self._request_metrics.append(_m)
                self._refresh_finish_events(
                    pool.finish(_rid, _m.mem_engine_id, _ft),
                )

            self._push_event(Event(
                time=ft, callback=_on_finish,
                request_id=rid, metrics=m,
            ))
