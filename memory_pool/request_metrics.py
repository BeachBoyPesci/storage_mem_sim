"""Memory access data contracts.

Pure data-contract layer for the multi-instance event path.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryRequestMetrics:
    """Metrics for a single completed request.

    Attributes:
        request_id: Identifier of the completed request.
        source_id: Access origin (e.g. ``prefill-0``).
        mem_engine_id: Instance the request ran on.
        arrival_time: Arrival time in seconds.
        finish_time: Completion time in seconds.
        size_bytes: Request size in bytes.
        latency: ``finish_time - arrival_time`` (s).
        standalone_time: ``size_bytes / effective_bandwidth`` (s).
        contention_delay: ``latency - standalone_time`` (s).
        average_bandwidth: ``size_bytes / latency`` (B/s).

    The formulas are evaluated by the caller (the engine);
    this container does not recompute them.
    """

    request_id: str
    source_id: str
    mem_engine_id: int
    arrival_time: float
    finish_time: float
    size: int
    latency: float
    standalone_time: float
    contention_delay: float
    average_bandwidth: float
