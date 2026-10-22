"""介质级请求数据结构。

MediaRequest 表示介质仿真粒度下的一次访问，由更高层的
MemoryRequest 拆分而来。
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class MediaRequest:
    """单个介质级访问请求。

    每个 MediaRequest 对应高层 MemoryRequest 中一个粒度大小的数据块。
    对 DRAM 仿真而言，通常是一次 cache line 访问；对 SSD 而言，
    是一个 sector 范围的访问。

    属性:
        addr: 本次访问的起始地址。
        addr_vec: 子地址向量（用于 bank/rank 级仿真）。
        req_type: 请求类型（0=读, 1=写）。
    """
    addr: int
    req_type: int
    addr_vec: List[int] = field(default_factory=list)
