"""Event — discrete simulation event types.

These types live in ``memory_pool`` so that both the pool and the
discrete-event simulator can import them without circular dependencies
(``des → memory_pool`` is allowed; the reverse is forbidden).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, TYPE_CHECKING

from ..memory_type import MemoryRequestType

if TYPE_CHECKING:
    from .memory_access import MemoryRequestMetrics


class EventKind(Enum):
    ARRIVAL = "arrival"
    FINISH = "finish"


@dataclass(frozen=True)
class Event:
    """One entry on the simulation event queue.

    For FINISH events, *metrics* carries the completed request's metrics
    so that Simulator does not need to call pool.finish().
    """

    time: float
    seq: int
    kind: EventKind
    source_id: str
    request_id: str
    addr: int
    size_bytes: int
    req_type: MemoryRequestType
    mem_engine_id: Optional[int] = None
    metrics: Optional["MemoryRequestMetrics"] = None
    # TODO(phase-2): add ``weight`` field for weighted bandwidth sharing.
