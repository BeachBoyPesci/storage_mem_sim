"""内存请求数据结构。

MemoryRequest 承载一次逻辑内存访问的全部信息，
包括地址、大小、请求类型，以及它分解为多少个
介质级操作的估计值。
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .memory_engine import MemoryEngineConfig
    from .media.media_request import MediaRequest
from .memory_type import MemoryRequestType


@dataclass
class MemoryRequest:
    """一次逻辑内存访问。

    构造时可传入 *config*，以便根据 engine 的 granularity
    自动计算 ``media_req_num``。
    """

    size: int
    req_type: MemoryRequestType
    addr: Optional[int] = None
    media_req_num: int = 0
    media_request_list: List["MediaRequest"] = field(default_factory=list)

    def __init__(
        self,
        addr: Optional[int],
        size: int,
        req_type: MemoryRequestType,
        *,
        config: Optional["MemoryEngineConfig"] = None,
    ):
        if addr is not None and addr < 0:
            raise ValueError(f"addr must be >= 0 or None, got {addr}")
        self.addr = addr
        self.size = size
        self.req_type = req_type
        g = config.granularity if config is not None else 64
        self.media_req_num = math.ceil(size / g) if g > 0 else 0
        self.media_request_list = []
