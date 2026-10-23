"""介质配置。

定义介质仿真层的配置参数。
"""

from dataclasses import dataclass, field
from .media_backend import MediaSystemBackend


@dataclass
class MediaConfig:
    """介质仿真后端的配置。

    属性：
        media_type: 使用哪个后端（ANALYTIC、RAMULATOR、MQSIM）。
        config_path: 后端专属配置文件的路径
                     （Ramulator 使用 YAML，MQSim 使用 XML，Analytic 不使用）。
        capacity: 设备容量，单位 GiB（实现按 1024**3 换算）。
        bandwidth: 峰值带宽，单位 GiB/s（Analytic 后端使用）。
        io_frequency: I/O 时钟频率，单位 MHz（Ramulator 用于将
                      cycles 转换为时间：scale_factor = 1.0 / (io_frequency * 10^6)）。
        granularity: 访问粒度，单位字节。Ramulator 会从 DRAM spec 自动推导；
                     其他后端使用该值作为回退。

        # MQSim 专属
        ssd_config_path: MQSim ssdconfig.xml 的路径。
        workload_config_path: MQSim workload.xml 的路径。
        request_size_bytes: 典型请求大小，单位字节。
                            较大时为带宽受限（bandwidth-bound），较小时为 IOPS 受限。
    """
    media_type: MediaSystemBackend = MediaSystemBackend.ANALYTIC
    config_path: str = ""
    capacity: float = 0.0
    bandwidth: float = 0.0
    io_frequency: float = 0.0
    granularity: int = 64

    # MQSim 专属
    ssd_config_path: str = ""
    workload_config_path: str = ""
    request_size_bytes: int = 131072
    merge_contiguous: bool = True

    @property
    def scale_factor(self) -> float:
        """Cycles -> 秒：1.0 / (io_frequency * 10^6)。"""
        if self.io_frequency > 0:
            return 1.0 / (self.io_frequency * 1e6)
        return 1.0
