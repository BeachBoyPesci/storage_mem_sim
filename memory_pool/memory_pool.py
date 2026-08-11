"""MemoryPool — multi-instance addressing, routing, and pool metrics."""

import bisect
import dataclasses
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple, TYPE_CHECKING

from ..memory_request import MemoryRequest
from ..memory_type import MemoryRequestType

if TYPE_CHECKING:
    from ..memory_config import MemoryEngineConfig
    from ..memory_engine import MemoryEngine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _EngineDesc:
    """Internal descriptor of one pool instance."""

    engine: "MemoryEngine"
    global_base: int
    capacity_bytes: int


@dataclass
class MemoryPoolConfig:
    """Configuration for a MemoryPool.

    Attributes:
        instance_count: Number of instances (>= 1).
    """

    instance_count: int

    def __post_init__(self):
        if self.instance_count < 1:
            raise ValueError(
                f"instance_count must be >= 1, got {self.instance_count}"
            )


class MemoryPool:
    """A pool of MemoryEngine instances with a global address space.

    The pool owns the global byte address space: each instance gets a fixed
    non-overlapping window ``[global_base, global_base + capacity)``. All
    pool-level addresses are global; instance-local addresses are derived
    by subtracting the window base.

    A request always targets the instance its data was placed on
    (allocation-time placement, not per-request balancing). Phase 1 does not
    support striping, migration, or replication across instances.
    """

    def __init__(
        self,
        instance_count: int,
        engine_config: "MemoryEngineConfig",
    ):
        """Build a pool of *instance_count* identical engines.

        TODO(phase-2): support pluggable allocation policies
        (LEAST_ALLOCATED, weighted, etc.).  Currently hard-coded to
        ROUND_ROBIN.

        TODO(phase-2): support heterogeneous instances (different
        capacities / bandwidths per engine).
        """
        if instance_count < 1:
            raise ValueError(
                f"instance_count must be >= 1, got {instance_count}"
            )

        # Build engines.
        self._engines: List[_EngineDesc] = []
        base = 0
        for idx in range(instance_count):
            cfg = dataclasses.replace(engine_config) if idx > 0 else engine_config
            from ..memory_engine import MemoryEngine
            engine = MemoryEngine(cfg)
            engine.instance_id = idx
            engine.global_base = base
            self._engines.append(_EngineDesc(
                engine=engine,
                global_base=base,
                capacity_bytes=engine.capacity_bytes,
            ))
            base += engine.capacity_bytes

        self._bases = [d.global_base for d in self._engines]
        self._rr_counter = 0

        # TODO(phase-2): POOL scope — shared bandwidth across engines.

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def instances(self) -> tuple:
        """Tuple of the pool's MemoryEngine instances."""
        return tuple(d.engine for d in self._engines)

    def get_engine(self, engine_id: int) -> "MemoryEngine":
        """Return the engine with the given instance id."""
        if not 0 <= engine_id < len(self._engines):
            raise IndexError(
                f"engine_id {engine_id} out of range for "
                f"{len(self._engines)} instances"
            )
        return self._engines[engine_id].engine

    def resolve_engine(self, addr: int, size_bytes: int) -> "MemoryEngine":
        """Resolve a global address range to its owning engine."""
        if addr < 0:
            raise ValueError(f"addr must be >= 0, got {addr}")
        if size_bytes <= 0:
            raise ValueError(f"size_bytes must be > 0, got {size_bytes}")
        idx = bisect.bisect_right(self._bases, addr) - 1
        if idx < 0:
            raise ValueError(f"addr {addr} is below the first window (base=0)")
        desc = self._engines[idx]
        end = addr + size_bytes
        if end > desc.global_base + desc.capacity_bytes:
            raise ValueError(
                f"request [0x{addr:x}, 0x{end:x}) spans beyond instance "
                f"{idx} window [0x{desc.global_base:x}, "
                f"0x{desc.global_base + desc.capacity_bytes:x}); "
                "a request must fit entirely inside one instance window"
            )
        return desc.engine

    # ------------------------------------------------------------------
    # Allocation
    # ------------------------------------------------------------------

    def get_tensor_addr(
        self,
        size_bytes: int,
        *,
        mem_engine_id: Optional[int] = None,
    ) -> Tuple[int, int]:
        """Allocate *size_bytes* and return ``(global_addr, engine_id)``.

        Round-robin: start at the last-selected engine and scan forward,
        picking the first one with enough remaining capacity.
        """
        if size_bytes <= 0:
            raise ValueError(f"size_bytes must be > 0, got {size_bytes}")

        n = len(self._engines)
        if mem_engine_id is not None:
            if not 0 <= mem_engine_id < n:
                raise ValueError(
                    f"mem_engine_id {mem_engine_id} out of range for "
                    f"{n} instances"
                )
            desc = self._engines[mem_engine_id]
            if desc.engine.remaining_capacity_bytes < self._aligned_size(desc.engine, size_bytes):
                raise ValueError(
                    f"engine {mem_engine_id}: remaining "
                    f"{desc.engine.remaining_capacity_bytes} B < "
                    f"aligned {size_bytes} B"
                )
            local_addr = desc.engine.get_tensor_addr(size_bytes)
            return desc.global_base + local_addr, desc.engine.instance_id

        # Round-robin: scan from _rr_counter, pick first that fits.
        # TODO(phase-2): pluggable allocation policies.
        start = self._rr_counter % n
        for offset in range(n):
            idx = (start + offset) % n
            desc = self._engines[idx]
            aligned = self._aligned_size(desc.engine, size_bytes)
            if desc.engine.remaining_capacity_bytes >= aligned:
                self._rr_counter = (idx + 1) % n
                local_addr = desc.engine.get_tensor_addr(size_bytes)
                return desc.global_base + local_addr, desc.engine.instance_id

        detail = ", ".join(
            f"engine {i}: capacity={d.capacity_bytes}, "
            f"remaining={d.engine.remaining_capacity_bytes}"
            for i, d in enumerate(self._engines)
        )
        raise ValueError(
            f"no instance has remaining capacity >= aligned {size_bytes} B; "
            f"[{detail}]"
        )


    @staticmethod
    def _aligned_size(engine: "MemoryEngine", size_bytes: int) -> int:
        return engine.align_up(size_bytes)

    # ------------------------------------------------------------------
    # Event interface
    # ------------------------------------------------------------------

    def submit(
        self, request: MemoryRequest, *, now: float,
    ) -> List[Tuple[str, float, "MemoryRequestMetrics"]]:
        """Submit one request; return [(rid, finish_time, metrics), ...].

        Routes to the correct engine.  *request.is_finish* determines
        whether this is an ARRIVAL (add request) or FINISH (remove +
        reallocate).  Pure routing — no event-queue logic.
        """
        if request.is_finish:
            engine = self.get_engine(request.mem_engine_id or 0)
            return engine.submit(request, now=now)

        if request.req_type == MemoryRequestType.KWRITE:
            if request.addr is not None:
                raise ValueError(
                    f"write request must have addr=None, got {request.addr}"
                )
            _, engine_id = self.get_tensor_addr(
                request.size, mem_engine_id=request.mem_engine_id,
            )
            engine = self.get_engine(engine_id)
        else:
            engine = self._validate_request(request)
        return engine.submit(request, now=now)

    def _validate_request(self, request: MemoryRequest) -> "MemoryEngine":
        """Validate the request and return its owning engine."""
        engine = self.resolve_engine(request.addr, request.size)
        if (
            request.mem_engine_id is not None
            and request.mem_engine_id != engine.instance_id
        ):
            raise ValueError(
                f"request addr 0x{request.addr:x} belongs to engine "
                f"{engine.instance_id}, but mem_engine_id "
                f"{request.mem_engine_id} was specified"
            )
        return engine