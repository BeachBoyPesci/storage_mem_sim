"""介质系统工厂。

根据配置中的 MediaSystemBackend 类型，以工厂模式创建介质仿真后端。
"""

import logging
from typing import Dict, Type

from .media_backend import MediaSystemBackend
from .media_config import MediaConfig
from .base_media import BaseMediaSystem

logger = logging.getLogger(__name__)


class MediaSystemFactory:
    """用于创建介质仿真后端实例的工厂。

    用法：
        config = MediaConfig(media_type=MediaSystemBackend.ANALYTIC, ...)
        media_system = MediaSystemFactory.create(config)
    """

    _backends: Dict[MediaSystemBackend, Type[BaseMediaSystem]] = {}

    @classmethod
    def register(cls, backend: MediaSystemBackend, system_cls: Type[BaseMediaSystem]):
        """为给定的后端类型注册对应的后端类。

        Args:
            backend: MediaSystemBackend 枚举值。
            system_cls: 与之关联的 BaseMediaSystem 子类。
        """
        cls._backends[backend] = system_cls

    @classmethod
    def create(cls, config: MediaConfig) -> BaseMediaSystem:
        """根据配置中的 media_type 创建介质系统实例。

        Args:
            config: 指定要创建哪种后端的 MediaConfig。

        Returns:
            相应 BaseMediaSystem 子类的实例。

        Raises:
            ValueError: 后端类型未注册时抛出。
        """
        backend = config.media_type
        if backend not in cls._backends:
            # 首次使用时尝试自动导入各后端
            cls._auto_register()
        if backend not in cls._backends:
            raise ValueError(
                f"Unknown media backend: {backend}. "
                f"Available backends: {list(cls._backends.keys())}"
            )
        simulator_class = cls._backends[backend]
        instance = simulator_class(config)
        logger.info("MediaSystem created: backend=%s class=%s",
                    backend.value, simulator_class.__name__)
        return instance

    @classmethod
    def _auto_register(cls):
        """惰性导入并注册所有内置后端实现。"""
        try:
            from .analytic_media_system import AnalyticMediaSystem
            cls._backends.setdefault(
                MediaSystemBackend.ANALYTIC, AnalyticMediaSystem
            )
        except ImportError:
            pass

        try:
            from .ramulator_media_system import RamulatorMediaSystem
            cls._backends.setdefault(
                MediaSystemBackend.RAMULATOR, RamulatorMediaSystem
            )
        except ImportError:
            pass

        try:
            from .mqsim_media_system import MQSimMediaSystem
            cls._backends.setdefault(
                MediaSystemBackend.MQSIM, MQSimMediaSystem
            )
        except ImportError:
            pass
