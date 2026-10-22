"""介质指标容器。

定义介质仿真层的单批次指标（MediaMetrics）和累计指标
（MediaSystemMetrics）结构。
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class MediaMetrics:
    """单次 handler_mem_request 调用返回的指标。

    属性：
        num_read_requests: 本批次中的读请求数量。
        num_write_requests: 本批次中的写请求数量。
        num_other_requests: 其他类型请求的数量。
        cycles: 消耗的总周期数（由 Ramulator 后端填写）。
        num_media_reqs: 已处理的介质级请求总数。
        time: 仿真时间，单位为秒（由 Analytic/MQSim 后端填写）。
        bandwidth: 带宽，单位为字节/秒（来自后端）。
        iops: MQSim 报告的端到端设备 IOPS；当后端不提供该指标时为 None。
        iops_read: MQSim 报告的读 IOPS。
        iops_write: MQSim 报告的写 IOPS。
    """
    num_read_requests: int = 0
    num_write_requests: int = 0
    num_other_requests: int = 0
    cycles: int = 0
    num_media_reqs: int = 0
    time: float = 0.0
    bandwidth: float = 0.0
    iops: Optional[float] = None
    iops_read: Optional[float] = None
    iops_write: Optional[float] = None

    def __add__(self, other: "MediaMetrics") -> "MediaMetrics":
        """合并计数器，并按时间加权合率指标。"""
        total_time = self.time + other.time

        def combined_rate(left: float, right: float) -> float:
            if total_time <= 0:
                return 0.0
            return (left * self.time + right * other.time) / total_time

        def combined_optional_rate(
            left: Optional[float], right: Optional[float]
        ) -> Optional[float]:
            if left is None:
                return right
            if right is None:
                return left
            return combined_rate(left, right)

        return MediaMetrics(
            num_read_requests=self.num_read_requests + other.num_read_requests,
            num_write_requests=self.num_write_requests + other.num_write_requests,
            num_other_requests=self.num_other_requests + other.num_other_requests,
            cycles=self.cycles + other.cycles,
            num_media_reqs=self.num_media_reqs + other.num_media_reqs,
            time=total_time,
            bandwidth=combined_rate(self.bandwidth, other.bandwidth),
            iops=combined_optional_rate(self.iops, other.iops),
            iops_read=combined_optional_rate(self.iops_read, other.iops_read),
            iops_write=combined_optional_rate(self.iops_write, other.iops_write),
        )


@dataclass
class MediaSystemMetrics:
    """跨多次 handler_mem_request 调用的累计指标。

    属性：
        num_read_requests: 累计读请求数。
        num_write_requests: 累计写请求数。
        num_other_requests: 累计其他请求数。
        cycles: 累计周期数。
        num_media_reqs: 累计介质请求数。
        time: 累计时间，单位为秒。
        bandwidth: 累计带宽（B/s）。
        iops: 按时间加权的 MQSim 设备 IOPS；其他后端为 None。
        iops_read: 按时间加权的 MQSim 读 IOPS。
        iops_write: 按时间加权的 MQSim 写 IOPS。
        media_metrics_list: 各批次 MediaMetrics 的历史记录。
    """
    num_read_requests: int = 0
    num_write_requests: int = 0
    num_other_requests: int = 0
    cycles: int = 0
    num_media_reqs: int = 0
    time: float = 0.0
    bandwidth: float = 0.0
    iops: Optional[float] = None
    iops_read: Optional[float] = None
    iops_write: Optional[float] = None
    media_metrics_list: List[MediaMetrics] = field(default_factory=list)

    def update_from_media(self, metrics: MediaMetrics):
        old_time = self.time

        # ---- 可累加计数器（跨批次求和）----
        self.num_read_requests += metrics.num_read_requests
        self.num_write_requests += metrics.num_write_requests
        self.num_other_requests += metrics.num_other_requests
        self.cycles += metrics.cycles
        self.num_media_reqs += metrics.num_media_reqs
        self.time += metrics.time
        self.media_metrics_list.append(metrics)

        # ---- 率指标（按时间加权平均）----
        if self.time <= 0:
            return

        if old_time > 0:
            # R_new = R_old × (T_old / T_new) + r_new × (t_new / T_new)
            old_weight = old_time / self.time
            new_weight = metrics.time / self.time
            self.iops = self._combine_optional_rate(
                self.iops, metrics.iops, old_weight, new_weight
            )
            self.iops_read = self._combine_optional_rate(
                self.iops_read, metrics.iops_read, old_weight, new_weight
            )
            self.iops_write = self._combine_optional_rate(
                self.iops_write, metrics.iops_write, old_weight, new_weight
            )
            self.bandwidth = (self.bandwidth * old_weight
                              + metrics.bandwidth * new_weight)
        else:
            # 第一个批次——直接用其值初始化
            self.iops = metrics.iops
            self.iops_read = metrics.iops_read
            self.iops_write = metrics.iops_write
            self.bandwidth = metrics.bandwidth

    @staticmethod
    def _combine_optional_rate(
        current: Optional[float],
        incoming: Optional[float],
        current_weight: float,
        incoming_weight: float,
    ) -> Optional[float]:
        if incoming is None:
            return current
        if current is None:
            return incoming
        return current * current_weight + incoming * incoming_weight
