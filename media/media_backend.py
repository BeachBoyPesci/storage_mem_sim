"""介质后端枚举。

定义支持的介质仿真后端类型。
"""

from enum import Enum


class MediaSystemBackend(Enum):
    """支持的介质仿真后端。

    ANALYTIC：基于 size/bandwidth 的纯解析模型（快速，低保真度）。
    RAMULATOR：通过 Ramulator2 进行周期精确（cycle-accurate）的 DRAM 仿真。
    MQSIM：通过 MQSim 进行事件驱动的 SSD 仿真。
    """
    ANALYTIC = "analytic"
    RAMULATOR = "ramulator"
    MQSIM = "mqsim"
