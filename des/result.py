"""SimulationResult — output of one discrete-event simulation run."""

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
