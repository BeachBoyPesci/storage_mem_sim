"""MediaSystem — 多后端内存/存储仿真层。

为内存/存储性能仿真提供可插拔的后端：
- AnalyticMediaSystem：纯解析式 roofline 估算
- RamulatorMediaSystem：基于 Ramulator2 的周期精确 DRAM 仿真
- MQSimMediaSystem：基于 MQSim 的事件驱动 SSD 仿真
"""

from .media_backend import MediaSystemBackend
from .media_config import MediaConfig
from .media_request import MediaRequest
from .media_metrics import MediaMetrics, MediaSystemMetrics
from .base_media import BaseMediaSystem
from .media_system_factory import MediaSystemFactory
from .analytic_media_system import AnalyticMediaSystem
from .ramulator_media_system import RamulatorMediaSystem
from .mqsim_media_system import MQSimMediaSystem

__all__ = [
    "MediaSystemBackend",
    "MediaConfig",
    "MediaRequest",
    "MediaMetrics",
    "MediaSystemMetrics",
    "BaseMediaSystem",
    "MediaSystemFactory",
    "AnalyticMediaSystem",
    "RamulatorMediaSystem",
    "MQSimMediaSystem",
]
