"""内存引擎配置。

定义 MemoryEngine 的配置参数。
容量值从 media_config.capacity 自动派生。
"""

from dataclasses import dataclass, field
from typing import Optional

from .memory_type import MemoryType


@dataclass
class MemoryEngineConfig:
    """MemoryEngine 的配置。

    Attributes:
        memory_type: 内存/存储介质类型（HBM、DDR、SSD、HBF）。仅作元数据
                     记录（日志、报告），不影响仿真行为。
        media_config: 介质后端配置。MemoryEngine 内部通过
                      MediaSystemFactory.create(media_config) 使用它。
                      必须设置 media_config.capacity（GB）。
        granularity: 地址对齐粒度，单位为字节。对 Ramulator 从 DRAM spec
                     自动派生；其他后端以此作为回退值。

    自动计算（从 media_config.capacity 派生，单实例口径）：
        capacity: 实例容量，单位为字节（= capacity_GB * 1024**3）。
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
