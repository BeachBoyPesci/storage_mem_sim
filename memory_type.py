"""内存类型与请求类型枚举定义。

定义 MemEngine 框架中使用的内存/存储介质类型和请求类型。
"""

from enum import Enum


class MemoryType(Enum):
    """内存/存储介质类型。"""
    HBM = "HBM"
    DDR = "DDR"
    SSD = "SSD"
    HBF = "HBF"


class MemoryRequestType(Enum):
    """内存请求类型。

    KREAD: 读请求（如加载权重、读取 KV cache）
    KWRITE: 写请求（如写回激活值、写入 KV cache）
    """
    KREAD = 0
    KWRITE = 1

    def to_media_req_type(self) -> int:
        """转换为介质层请求类型（0=读，1=写）。"""
        return self.value
