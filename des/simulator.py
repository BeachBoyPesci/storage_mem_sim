"""SimpleSimulator — thin DES adapter that drives a MemoryPool."""

import heapq
import logging
from typing import Dict, List, Optional, Set, Tuple, TYPE_CHECKING

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
        # Rids whose completion metrics were already recorded.  Required for
        # correctness, not just defense: _latest_finish oscillation
        # (ft -> ft' -> ft) can leave two same-time events for one rid on the
        # heap, and the stale check in run() cannot tell same-time copies
        # apart — the second fire would double-count without this set.
        self._recorded: Set[str] = set()

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

    def _push_event(self, event: Event) -> None:
        self._seq += 1
        object.__setattr__(event, 'seq', self._seq)
        heapq.heappush(self._events, (event.time, event.seq, event))

    def _refresh_finish_events(self, entries: list) -> None:
        """Schedule FINISH events for a full prediction snapshot.

        *entries* is every active request returned by ``pool.submit``/
        ``pool.finish`` (empty = idle).  An entry whose finish time equals
        the latest prediction for its rid is skipped: an identical event is
        already queued, and pushing a duplicate would fire twice at the same
        time and double-report the completion.  Predictions at *different*
        times replace the old event lazily — the old one stays in the heap
        and is skipped on pop (stale check in ``run()``).

        Because every engine state change returns the full snapshot, every
        queued event equals its request's latest prediction, so an event
        fires exactly at the request's true completion.  The simulator never
        needs to second-guess a completion — stale-skipping above is the only
        validity check.
        """
        pool = self._memory_pool
        for entry in entries:
            m = entry.metrics
            rid = m.request_id
            ft = m.finish_time
            if self._latest_finish.get(rid) == ft:
                # Identical event already queued — dedupe.
                continue
            # Record latest prediction — old FINISH events for this rid
            # are now stale and will be skipped on pop.
            self._latest_finish[rid] = ft

            def _on_finish(_m=m, _rid=rid, _ft=ft) -> None:
                # The event fired at the request's latest prediction — a
                # true completion.  Record the metrics captured at
                # scheduling (_recorded guards same-rid same-time copies
                # created by finish-time oscillation).
                if _m is not None and _rid not in self._recorded:
                    self._request_metrics.append(_m)
                    self._recorded.add(_rid)
                # The engine popped the request; refresh from the snapshot
                # of the survivors (may be empty).
                self._refresh_finish_events(
                    pool.finish(_rid, _m.mem_engine_id, _ft),
                )

            self._push_event(Event(
                time=ft, callback=_on_finish,
                request_id=rid, metrics=m,
            ))
