"""内存引擎指标容器。

为 MemoryEngine 定义单次请求（MemoryMetrics）和累计
（MemoryEngineMetrics）两类指标结构。
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class MemoryMetrics:
    """单次 issue_request 调用的指标。

    Attributes:
        cycles: 消耗的总周期数（来自介质 backend）。
        total_time: 总时间，单位为秒（来自介质 backend）。
        memory_reqs_num: 被模拟存储实例中的 engine 级请求数。
        iops: MQSim 报告的端到端设备 IOPS；当所选 backend
              不提供该指标时为 None。pool 聚合时为各实例非 None
              值的平均值（IOPS 是单实例设备属性，不求和）。
        bandwidth: 带宽，单位为字节/秒（来自介质 backend）。
    """
    cycles: int = 0
    total_time: float = 0.0
    memory_reqs_num: int = 0
    bandwidth: float = 0.0
    iops: Optional[float] = None
    iops_read: Optional[float] = None
    iops_write: Optional[float] = None

@dataclass
class MemoryEngineMetrics:
    """跨所有 issue_request 调用的累计指标。

    Attributes:
        cycles: 累计周期数。
        total_time: 累计总时间，单位为秒。
        total_bytes: 被模拟存储实例累计传输的字节数。
        memory_reqs_num: 被模拟存储实例中累计的 engine 级请求数。
        mem_metrics_list: 每次请求的 MemoryMetrics 历史记录。
        bandwidth: 累计带宽 = total_bytes / total_time (B/s)。
        iops: 按时间加权的端到端 MQSim 设备 IOPS；对不报告设备
              IOPS 的 backend 为 None。
    """
    cycles: int = 0
    total_time: float = 0.0
    total_bytes: int = 0
    memory_reqs_num: int = 0
    bandwidth: float = 0.0
    iops: Optional[float] = None
    iops_read: Optional[float] = None
    iops_write: Optional[float] = None
    mem_metrics_list: List[MemoryMetrics] = field(default_factory=list)

    def update(self, metrics: MemoryMetrics, total_bytes: int):
        old_time = self.total_time

        # ---- 可加性计数器 ----
        self.cycles += metrics.cycles
        self.total_time += metrics.total_time
        self.total_bytes += total_bytes
        self.memory_reqs_num += metrics.memory_reqs_num
        self.mem_metrics_list.append(metrics)

        if self.total_time <= 0:
            return

        # ---- 带宽：total_bytes / total_time（精确计算，始终有效）----
        self.bandwidth = self.total_bytes / self.total_time

        # IOPS 有意仅由 MQSim 提供。该值是每个仿真批次报告的端到端
        # 设备速率，因此累计结果必须按各批次的仿真时间加权。
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
