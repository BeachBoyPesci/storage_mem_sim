"""Analytic 介质系统 —— 纯 roofline 估算。

按 total_data_size / bandwidth 计算访问时间，不做周期级仿真。
适用于快速原型验证和早期阶段分析。
"""

import logging
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory_request import MemoryRequest

from .base_media import BaseMediaSystem
from .media_config import MediaConfig
from .media_metrics import MediaMetrics
from ..memory_type import MemoryRequestType

logger = logging.getLogger(__name__)


class AnalyticMediaSystem(BaseMediaSystem):
    """纯解析式后端，使用 size/bandwidth 进行估算。

    总访问时间按每个请求的 (size / bandwidth) 求和计算。这是一个零精度
    模型 —— 忽略所有排队、争用、调度和器件级时序效应。

    用法：
        config = MediaConfig(
            media_type=MediaSystemBackend.ANALYTIC,
            bandwidth=100.0,      # GiB/s
        )
        sys = AnalyticMediaSystem(config)
        metrics = sys.handler_mem_request(mem_req_list)
    """

    def __init__(self, config: MediaConfig):
        super().__init__(config)
        if config.bandwidth <= 0:
            raise ValueError(
                f"bandwidth must be > 0 for Analytic backend, got {config.bandwidth}"
            )
        # 将 bandwidth 从 GiB/s 转换为 B/s。
        self._bandwidth_bytes_per_sec = config.bandwidth * (1024 ** 3)
        logger.info("Analytic backend ready: bandwidth=%.1f GiB/s", config.bandwidth)

    def handler_mem_request(
        self, mem_req_list: List["MemoryRequest"]
    ) -> MediaMetrics:
        """按 total_bytes / bandwidth 估算总时间。

        参数：
            mem_req_list: MemoryRequest 对象列表。

        返回：
            填充了时间和请求计数的 MediaMetrics。
        """
        total_bytes = 0
        num_read = 0
        num_write = 0

        for mem_req in mem_req_list:
            total_bytes += mem_req.size

            if mem_req.req_type == MemoryRequestType.KREAD:
                num_read += 1
            elif mem_req.req_type == MemoryRequestType.KWRITE:
                num_write += 1

        total_time = total_bytes / self._bandwidth_bytes_per_sec
        metrics = MediaMetrics(
            num_read_requests=num_read,
            num_write_requests=num_write,
            num_other_requests=0,
            cycles=0,
            num_media_reqs=len(mem_req_list),
            time=total_time,
            bandwidth=self._bandwidth_bytes_per_sec if total_time > 0 else 0.0,
        )

        self.system_metrics.update_from_media(metrics)
        logger.debug(
            "Analytic handler: time=%.4fus reads=%d writes=%d "
            "total_bytes=%d",
            total_time * 1e6, num_read, num_write,
            total_bytes)
        return metrics
