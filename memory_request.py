"""Memory request data structure.

MemoryRequest carries all the information for one logical memory access,
including address, size, request type, and an estimate of how many
media-level operations it decomposes into.
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .memory_config import MemoryEngineConfig
    from .media.media_request import MediaRequest
from .memory_type import MemoryRequestType


@dataclass
class MemoryRequest:
    """One logical memory access.

    Replaces the old ``MemoryObject`` wrapper — all fields are now
    first-class on the request.  *config* may be passed at construction
    time to auto-compute ``media_req_num``; omit it to default to 0
    (event path).
    """

    size: int
    req_type: MemoryRequestType
    addr: Optional[int] = None
    media_req_num: int = 0
    media_request_list: List["MediaRequest"] = field(default_factory=list)

    # Event path fields.
    request_id: str = ""
    source_id: str = ""
    mem_engine_id: Optional[int] = None
    is_finish: bool = False

    def __init__(
        self,
        addr: Optional[int],
        size: int,
        req_type: MemoryRequestType,
        *,
        config: Optional["MemoryEngineConfig"] = None,
        request_id: str = "",
        source_id: str = "",
        mem_engine_id: Optional[int] = None,
        is_finish: bool = False,
    ):
        if addr is not None and addr < 0:
            raise ValueError(f"addr must be >= 0 or None, got {addr}")
        self.addr = addr
        self.size = size
        self.req_type = req_type
        g = config.granularity if config is not None else 64
        self.media_req_num = math.ceil(size / g) if g > 0 else 0
        self.media_request_list = []
        self.request_id = request_id
        self.source_id = source_id
        self.mem_engine_id = mem_engine_id
        self.is_finish = is_finish

