"""Memory Engine —— 内存请求生命周期管理的核心模块。

MemoryEngine 对一个物理介质实例建模。它负责地址分配、请求构造，
并通过同步的 ``issue_request()`` 批量路径，把性能仿真委托给配置的
MediaSystem 后端。
"""

import math
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from .memory_type import MemoryRequestType, MemoryType
from .memory_request import MemoryRequest
from .memory_metrics import MemoryMetrics, MemoryEngineMetrics

logger = logging.getLogger(__name__)


@dataclass
class MemoryEngineConfig:
    """MemoryEngine 的配置。

    Attributes:
        memory_type: 内存/存储介质类型（HBM、DDR、SSD、HBF）。仅作元数据
                     记录（日志、报告），不影响仿真行为。
        media_config: 介质后端配置。MemoryEngine 内部通过
                      MediaSystemFactory.create(media_config) 使用它。
                      必须设置 media_config.capacity（GiB）。
        granularity: 地址对齐粒度，单位为字节。对 Ramulator 从 DRAM spec
                     自动派生；其他后端以此作为回退值。

    自动计算（从 media_config.capacity 派生，单实例口径）：
        capacity: 实例容量，单位为字节（= capacity_GiB * 1024**3）。
    """
    # TODO: memory_type 是调用方自声明的标签，与后端实际仿真的器件没有
    # 一致性校验——ramulator 的器件由 YAML preset 决定、mqsim 固定为 SSD，
    # 声明值可能与二者矛盾（如 memory_type=SSD 配 ramulator 后端）。
    # 出报告时应同时记录后端类型，或改为由后端自报介质信息。
    memory_type: MemoryType = MemoryType.HBM
    media_config: Optional[object] = None   # MediaConfig，使用前必须设置
    granularity: int = field(default=0, init=False)  # 由 MemoryEngine 根据后端设置

    # 在 __post_init__ 中根据 media_config.capacity 自动计算
    capacity: int = field(default=0, init=False)

    def __post_init__(self):
        if self.media_config is not None:
            self.capacity = int(self.media_config.capacity * 1024 ** 3)


class MemoryEngine:
    """单个物理介质实例。

    ``issue_request()`` 把一批请求委托给配置的 MediaSystem 后端做
    吞吐量仿真。多实例寻址和路由位于 ``memory_pool.MemoryPool``。
    """

    def __init__(self, mem_config: MemoryEngineConfig):
        """创建 engine 及其 MediaSystem 后端。

        使用 MediaSystemFactory 从 *mem_config.media_config* 实例化
        后端，并从后端推导地址粒度（例如 Ramulator 的 ``_tx_bytes``，
        否则为 64 B）。
        """
        self.mem_config = mem_config
        self.global_addr: int = 0
        self.engine_metrics = MemoryEngineMetrics()

        self.instance_id: int = 0
        self.global_base: int = 0

        if mem_config.media_config is None:
            raise ValueError("MemoryEngineConfig.media_config is required")
        from .media.media_system_factory import MediaSystemFactory
        self.media_system = MediaSystemFactory.create(mem_config.media_config)

        tx = getattr(self.media_system, '_tx_bytes', None)
        self.mem_config.granularity = tx if tx else 64

        logger.info(
            "MemoryEngine init: mem_type=%s granularity=%d capacity=%dGiB",
            mem_config.memory_type.value, self.mem_config.granularity,
            mem_config.capacity // (1024 ** 3))

    # ------------------------------------------------------------------
    # 实例元数据
    # ------------------------------------------------------------------

    @property
    def capacity_bytes(self) -> int:
        return self.mem_config.capacity

    # ------------------------------------------------------------------
    # 地址分配
    # ------------------------------------------------------------------

    def align_up(self, size: int) -> int:
        """将 *size* 向上对齐到 engine 的地址粒度。

        Returns:
            对齐后的大小（向上取整到粒度边界）。
        """
        step = self.mem_config.granularity
        return math.ceil(size / step) * step

    def get_tensor_addr(self, size: int) -> int:
        """为一个 tensor 分配对齐后的地址。

        本地地址计数器按对齐后的大小推进。
        如果分配会超出容量，抛出 OverflowError。

        Returns:
            该 tensor 的起始本地地址。
        """
        aligned_size = self.align_up(size)
        tensor_addr = self.global_addr
        self.global_addr += aligned_size
        if self.mem_config.capacity > 0 and self.global_addr > self.mem_config.capacity:
            raise OverflowError(
                f"Address overflow: global_addr {self.global_addr} exceeds "
                f"capacity {self.mem_config.capacity}"
            )
        return tensor_addr

    def reset_addr(self):
        """将本地地址计数器重置为零。"""
        self.global_addr = 0

    # ------------------------------------------------------------------
    # 同步批量路径
    # ------------------------------------------------------------------

    def issue_request(
        self,
        addr: List[int],
        size: List[int],
        req_type: List[MemoryRequestType],
    ) -> MemoryMetrics:
        """同步执行一批请求。

        三个列表必须等长。每个请求都以本实例为目标（无 DP 复制、
        无跨实例路由）。返回单次请求与累计指标。
        """
        if self.media_system is None:
            raise RuntimeError("No media_system configured.")
        n = len(addr)
        if len(size) != n or len(req_type) != n:
            raise ValueError("addr, size, req_type must have the same length")
        for i in range(n):
            if addr[i] < 0:
                raise ValueError(f"addr[{i}] must be >= 0")
            if size[i] <= 0:
                raise ValueError(f"size[{i}] must be > 0")
        if n == 0:
            return MemoryMetrics()
        mem_reqs = [
            MemoryRequest(addr[i], size[i], req_type[i], config=self.mem_config)
            for i in range(n)
        ]
        total_media_metrics = self.media_system.handler_mem_request(mem_reqs)
        simulated_bytes = sum(req.size for req in mem_reqs)
        mem_metrics = MemoryMetrics(
            cycles=total_media_metrics.cycles,
            total_time=total_media_metrics.time,
            memory_reqs_num=n,
            bandwidth=total_media_metrics.bandwidth,
            iops=total_media_metrics.iops,
            iops_read=total_media_metrics.iops_read,
            iops_write=total_media_metrics.iops_write,
        )
        self.engine_metrics.update(mem_metrics, simulated_bytes)
        return mem_metrics

    def get_engine_metrics(self) -> MemoryEngineMetrics:
        """返回同步路径的累计指标。"""
        return self.engine_metrics

    def reset_engine_metrics(self):
        """重置同步路径的累计指标。"""
        self.engine_metrics = MemoryEngineMetrics()
