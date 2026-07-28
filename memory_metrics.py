"""Memory engine metrics containers.

Defines per-request (MemoryMetrics) and cumulative (MemoryEngineMetrics)
metrics structures for the MemoryEngine.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class MemoryMetrics:
    """Metrics for a single issue_request call.

    Attributes:
        cycles: Total cycles consumed (from media backend).
        total_time: Total time in seconds (from media backend).
        memory_scale_factor: Scale factor applied for time conversion.
        memory_reqs_num: Number of engine-level requests in the simulated
                         storage instance.
        global_memory_reqs_num: Total engine-level requests across all
                                DP ranks and storage instances.
        iops: End-to-end device IOPS reported by MQSim, or None when the
              selected backend does not provide this metric.
        bandwidth: Bandwidth in bytes/second (from media backend).
    """
    cycles: int = 0
    total_time: float = 0.0
    memory_scale_factor: int = 1
    memory_reqs_num: int = 0
    global_memory_reqs_num: int = 0
    bandwidth: float = 0.0
    iops: Optional[float] = None
    iops_read: Optional[float] = None
    iops_write: Optional[float] = None

@dataclass
class MemoryEngineMetrics:
    """Cumulative metrics across all issue_request calls.

    Attributes:
        cycles: Accumulated cycles.
        total_time: Accumulated total time in seconds.
        total_bytes: Accumulated bytes transferred by the simulated
                     storage instance.
        memory_reqs_num: Accumulated engine-level requests in the
                         simulated storage instance.
        global_memory_reqs_num: Accumulated engine-level requests
                                across all DP ranks and instances.
        mem_metrics_list: History of per-request MemoryMetrics.
        bandwidth: Cumulative bandwidth = total_bytes / total_time (B/s).
        iops: Time-weighted end-to-end MQSim device IOPS, or None for
              backends that do not report device IOPS.
    """
    cycles: int = 0
    total_time: float = 0.0
    total_bytes: int = 0
    memory_reqs_num: int = 0
    global_memory_reqs_num: int = 0
    bandwidth: float = 0.0
    iops: Optional[float] = None
    iops_read: Optional[float] = None
    iops_write: Optional[float] = None
    mem_metrics_list: List[MemoryMetrics] = field(default_factory=list)

    def update(self, metrics: MemoryMetrics, total_bytes: int):
        old_time = self.total_time

        # ---- additive counters ----
        self.cycles += metrics.cycles
        self.total_time += metrics.total_time
        self.total_bytes += total_bytes
        self.memory_reqs_num += metrics.memory_reqs_num
        self.global_memory_reqs_num += metrics.global_memory_reqs_num
        self.mem_metrics_list.append(metrics)

        if self.total_time <= 0:
            return

        # ---- bandwidth: total_bytes / total_time (exact, always works) ----
        self.bandwidth = self.total_bytes / self.total_time

        # IOPS is intentionally MQSim-only.  The value is the end-to-end
        # device rate reported for each simulation batch, so cumulative
        # results must be weighted by the batch simulation time.
        self.iops = self._combine_optional_rate(
            self.iops, metrics.iops, old_time, metrics.total_time
        )
        self.iops_read = self._combine_optional_rate(
            self.iops_read, metrics.iops_read, old_time, metrics.total_time
        )
        self.iops_write = self._combine_optional_rate(
            self.iops_write, metrics.iops_write, old_time, metrics.total_time
        )

    @staticmethod
    def _combine_optional_rate(
        current: Optional[float],
        incoming: Optional[float],
        current_time: float,
        incoming_time: float,
    ) -> Optional[float]:
        if incoming is None:
            return current
        if current is None or current_time <= 0:
            return incoming
        combined_time = current_time + incoming_time
        if combined_time <= 0:
            return incoming
        return (
            current * current_time + incoming * incoming_time
        ) / combined_time
