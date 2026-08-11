"""Memory access data contracts.

Event-driven multi-instance access types shared between the MemoryPool,
the engine, and the parent discrete-event simulator.

This module is a pure data-contract layer: it depends only on
``memory_type`` and must not import engine, pool, or simulation types.
"""

from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from ..memory_type import MemoryRequestType

if TYPE_CHECKING:
    pass  # MemoryAccess used by ActiveMemoryRequest via TYPE_CHECKING


@dataclass(frozen=True)
class MemoryAccess:
    """A logical access submitted to the MemoryPool by the parent DES.

    Attributes:
        request_id: Identifier unique within the parent simulation.
        source_id: Access origin (e.g. ``prefill-0``).
        addr: Pool-global byte address.
        size_bytes: Access size in bytes.
        req_type: KREAD or KWRITE.
        mem_engine_id: Optional instance constraint.

    The arrival time is not part of the access; it is expressed by the
    parent event and the ``now=...`` argument of ``submit()``.

    TODO(phase-2): add ``weight`` field for weighted bandwidth sharing.
    """

    request_id: str
    source_id: str
    addr: int
    size_bytes: int
    req_type: MemoryRequestType
    mem_engine_id: Optional[int] = None
    is_finish: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError(
                "request_id must be a non-empty string, "
                f"got {self.request_id!r}"
            )
        if not isinstance(self.source_id, str) or not self.source_id:
            raise ValueError(
                "source_id must be a non-empty string, "
                f"got {self.source_id!r}"
            )
        if self.addr < 0:
            raise ValueError(f"addr must be >= 0, got {self.addr}")
        if self.size_bytes <= 0:
            raise ValueError(f"size_bytes must be > 0, got {self.size_bytes}")
        if self.mem_engine_id is not None and self.mem_engine_id < 0:
            raise ValueError(
                f"mem_engine_id must be >= 0 or None, "
                f"got {self.mem_engine_id}"
            )
        if not isinstance(self.req_type, MemoryRequestType):
            raise ValueError(
                "req_type must be MemoryRequestType, "
                f"got {self.req_type!r}"
            )
        if self.mem_engine_id is not None and self.mem_engine_id < 0:
            raise ValueError(
                f"mem_engine_id must be >= 0 or None, "
                f"got {self.mem_engine_id}"
            )


@dataclass
class ActiveMemoryRequest:
    """Internal mutable state of one in-flight request.

    Attributes:
        access: The submitted MemoryAccess.
        local_addr: Instance-local byte address.
        mem_engine_id: Engine the request was submitted to.
        arrival_time: Arrival time in seconds.
        remaining_bytes: Bytes still to transfer (B).
        allocated_bandwidth: Bandwidth currently allocated (B/s).
    """

    access: MemoryAccess
    local_addr: int
    mem_engine_id: int
    arrival_time: float
    remaining_bytes: float
    allocated_bandwidth: float = 0.0


@dataclass(frozen=True)
class MemoryRequestMetrics:
    """Metrics for the single request completed by ``finish()``.

    Attributes:
        request_id: Identifier of the completed request.
        source_id: Access origin (e.g. ``prefill-0``).
        mem_engine_id: Instance the request ran on.
        arrival_time: Arrival time in seconds.
        finish_time: Completion time in seconds.
        size_bytes: Request size in bytes.
        latency: ``finish_time - arrival_time`` (s).
        standalone_time: ``size_bytes / effective_bandwidth`` (s).
        contention_delay: ``latency - standalone_time`` (s).
        average_bandwidth: ``size_bytes / latency`` (B/s).

    The formulas are evaluated by the caller (the engine);
    this container does not recompute them.
    """

    request_id: str
    source_id: str
    mem_engine_id: int
    arrival_time: float
    finish_time: float
    size_bytes: int
    latency: float
    standalone_time: float
    contention_delay: float
    average_bandwidth: float
