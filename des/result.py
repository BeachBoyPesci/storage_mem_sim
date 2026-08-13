"""SimulationResult — output of one discrete-event simulation run."""

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..memory_pool import MemoryRequestMetrics


@dataclass
class SimulationResult:
    """Aggregated output from a SimpleSimulator run."""

    request_metrics: List[MemoryRequestMetrics] = field(default_factory=list)
    per_source: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    makespan: float = 0.0
    scheduled_finish_events: int = 0
    stale_finish_events: int = 0

    def save_json(self, filepath: str) -> None:
        """Write results as JSON to *filepath*."""
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, "w") as f:
            json.dump({
                "makespan_s": self.makespan,
                "scheduled_finish_events": self.scheduled_finish_events,
                "stale_finish_events": self.stale_finish_events,
                "per_source": {
                    s: {
                        "avg_latency_s": v["avg_latency"],
                        "avg_contention_delay_s": v["avg_contention_delay"],
                        "total_bytes": v["total_bytes"],
                        "count": v["count"],
                    }
                    for s, v in self.per_source.items()
                },
                "request_metrics": [
                    {
                        "request_id": m.request_id,
                        "source_id": m.source_id,
                        "mem_engine_id": m.mem_engine_id,
                        "arrival_time_s": m.arrival_time,
                        "finish_time_s": m.finish_time,
                        "size_bytes": m.size,
                        "latency_s": m.latency,
                        "standalone_time_s": m.standalone_time,
                        "contention_delay_s": m.contention_delay,
                        "average_bandwidth_Bps": m.average_bandwidth,
                    }
                    for m in self.request_metrics
                ],
            }, f, indent=2)

    def save_trace(self, filepath: str) -> None:
        """Write results as Chrome Tracing JSON to *filepath*.

        Open ``chrome://tracing`` in Chrome and drag the file in.
        """
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)

        events = []
        # Metadata: rename Process → Engine N, Thread → source_id.
        engine_ids = sorted(set(m.mem_engine_id for m in self.request_metrics))
        for eid in engine_ids:
            events.append({
                "ph": "M", "pid": eid,
                "name": "process_name", "args": {"name": f"Engine {eid}"},
            })
        source_ids = sorted(set(m.source_id for m in self.request_metrics))
        for sid in source_ids:
            for m in self.request_metrics:
                if m.source_id == sid:
                    events.append({
                        "ph": "M", "pid": m.mem_engine_id,
                        "name": "thread_name", "args": {"name": sid},
                    })
                    break

        for m in self.request_metrics:
            events.append({
                "name": f"{m.source_id} ({m.size:,}B)",
                "cat": f"eng {m.mem_engine_id}",
                "ph": "X",
                "ts": m.arrival_time * 1e6,
                "dur": m.latency * 1e6,
                "pid": m.mem_engine_id,
                "tid": m.request_id,
                "args": {
                    "request_id": m.request_id,
                    "arrival_ns": f"{m.arrival_time * 1e9:.1f}",
                    "finish_ns": f"{m.finish_time * 1e9:.1f}",
                    "latency_ns": f"{m.latency * 1e9:.1f}",
                    "standalone_ns": f"{m.standalone_time * 1e9:.1f}",
                    "contention_ns": f"{m.contention_delay * 1e9:.1f}",
                    "average_bw_GBs": f"{m.average_bandwidth / 1e9:.2f}",
                },
            })
        with open(filepath, "w") as f:
            json.dump({"traceEvents": events, "displayTimeUnit": "ns"}, f, indent=2)
