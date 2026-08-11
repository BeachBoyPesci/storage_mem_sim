"""Event — callback-driven discrete event types."""

from dataclasses import dataclass, field
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory_pool import MemoryRequestMetrics


@dataclass(frozen=True)
class Event:
    """One entry on the simulation event queue.

    *callback* is called when the event fires.  The difference between
    ARRIVAL and FINISH is encoded in the callback body, not in an enum.
    """

    time: float
    callback: Callable[[], None]
    request_id: str
    metrics: Optional["MemoryRequestMetrics"] = None
    seq: int = 0
