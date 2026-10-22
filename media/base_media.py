"""介质仿真系统的抽象基类。

所有介质仿真后端都必须继承 BaseMediaSystem 并实现
handler_mem_request 方法。
"""

from abc import ABC, abstractmethod
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory_request import MemoryRequest
from .media_config import MediaConfig
from .media_metrics import MediaMetrics


class BaseMediaSystem(ABC):
    """所有介质仿真后端的抽象基类。

    子类必须实现 handler_mem_request，用于处理一批 MemoryRequest
    对象并返回性能指标。

    Attributes:
        config: 介质配置（后端类型、路径、参数）。
        system_metrics: 累计指标累加器。
    """

    def __init__(self, config: MediaConfig):
        """使用给定配置初始化介质系统。

        Args:
            config: 包含后端设置的 MediaConfig。
        """
        self.config = config
        # 在此处导入以避免模块加载时的循环依赖
        from .media_metrics import MediaSystemMetrics
        self.system_metrics = MediaSystemMetrics()

    @abstractmethod
    def handler_mem_request(
        self, mem_req_list: List["MemoryRequest"]
    ) -> MediaMetrics:
        """处理一批内存请求并返回性能指标。

        Args:
            mem_req_list: 待仿真的 MemoryRequest 对象列表。

        Returns:
            包含仿真结果的 MediaMetrics。
        """
        ...

    def get_system_metrics(self):
        """返回累计系统指标。

        Returns:
            带有累计历史数据的 MediaSystemMetrics。
        """
        return self.system_metrics

    def reset_system_metrics(self):
        """重置累计系统指标。"""
        from .media_metrics import MediaSystemMetrics
        self.system_metrics = MediaSystemMetrics()
