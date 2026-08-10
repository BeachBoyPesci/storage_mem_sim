"""MemoryPool — multi-instance addressing, routing, and pool metrics."""

import bisect
import dataclasses
import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple, TYPE_CHECKING

from .memory_access import MemoryAccess
from ..media.media_backend import MediaSystemBackend
from .allocation_policy import AllocationPolicy
from .event import Event

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
        allocation_policy: Placement policy (default LEAST_ALLOCATED).
    """

    instance_count: int
    allocation_policy: AllocationPolicy = AllocationPolicy.LEAST_ALLOCATED

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
    (allocation-time placement, not per-access balancing). Phase 1 does not
    support striping, migration, or replication across instances.
    """

    def __init__(
        self,
        engines: Sequence["MemoryEngine"],
        *,
        allocation_policy: AllocationPolicy = AllocationPolicy.LEAST_ALLOCATED,
    ):
        if not engines:
            raise ValueError("MemoryPool requires at least one engine")
        if not all(
            e.media_system.config.media_type is MediaSystemBackend.ANALYTIC
            for e in engines
        ):
            raise ValueError(
                "MemoryPool currently only supports the Analytic backend"
            )

        self.allocation_policy = allocation_policy

        # Fixed global address windows (cumulative capacity).
        self._descs: List[_EngineDesc] = []
        base = 0
        for idx, engine in enumerate(engines):
            engine.instance_id = idx
            engine.global_base = base
            self._descs.append(_EngineDesc(
                engine=engine,
                global_base=base,
                capacity_bytes=engine.capacity_bytes,
            ))
            base += engine.capacity_bytes

        self._bases = [d.global_base for d in self._descs]
        self._rr_counter = 0

        # TODO(phase-2): POOL scope — shared bandwidth across engines.

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_homogeneous(
        cls,
        instance_count: int,
        engine_config: "MemoryEngineConfig",
        *,
        allocation_policy: AllocationPolicy = AllocationPolicy.LEAST_ALLOCATED,
    ) -> "MemoryPool":
        """Build a pool of identical instances from one engine config."""
        if instance_count < 1:
            raise ValueError(
                f"instance_count must be >= 1, got {instance_count}"
            )
        engines = [
            _make_engine(dataclasses.replace(engine_config))
            for _ in range(instance_count)
        ]
        return cls(
            engines,
            allocation_policy=allocation_policy,
        )

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def instances(self) -> tuple:
        """Tuple of the pool's MemoryEngine instances."""
        return tuple(d.engine for d in self._descs)

    def get_engine(self, engine_id: int) -> "MemoryEngine":
        """Return the engine with the given instance id."""
        if not 0 <= engine_id < len(self._descs):
            raise IndexError(
                f"engine_id {engine_id} out of range for "
                f"{len(self._descs)} instances"
            )
        return self._descs[engine_id].engine

    def resolve_engine(self, addr: int, size_bytes: int) -> "MemoryEngine":
        """Resolve a global address range to its owning engine."""
        if addr < 0:
            raise ValueError(f"addr must be >= 0, got {addr}")
        if size_bytes <= 0:
            raise ValueError(f"size_bytes must be > 0, got {size_bytes}")
        idx = bisect.bisect_right(self._bases, addr) - 1
        if idx < 0:
            raise ValueError(f"addr {addr} is below the first window (base=0)")
        desc = self._descs[idx]
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
    ) -> int:
        """Allocate a tensor and return its pool-global byte address."""
        if size_bytes <= 0:
            raise ValueError(f"size_bytes must be > 0, got {size_bytes}")

        if mem_engine_id is not None:
            candidates = [self._descs[mem_engine_id]] \
                if 0 <= mem_engine_id < len(self._descs) else []
            if not candidates:
                raise ValueError(
                    f"mem_engine_id {mem_engine_id} out of range for "
                    f"{len(self._descs)} instances"
                )
        else:
            candidates = list(self._descs)

        aligned = [self._aligned_size(d.engine, size_bytes) for d in candidates]
        viable = [
            (d, a) for d, a in zip(candidates, aligned)
            if d.engine.remaining_capacity_bytes >= a
        ]
        if not viable:
            detail = ", ".join(
                f"engine {i}: capacity={d.capacity_bytes}, "
                f"remaining={d.engine.remaining_capacity_bytes}"
                for i, d in enumerate(self._descs)
            )
            raise ValueError(
                f"no instance has remaining capacity >= aligned {size_bytes} "
                f"B (aligned {aligned[0] if aligned else size_bytes} B); "
                f"[{detail}]"
            )

        if self.allocation_policy is AllocationPolicy.ROUND_ROBIN:
            desc, _ = viable[self._rr_counter % len(viable)]
            self._rr_counter += 1
        else:
            desc = min(viable, key=lambda dv: dv[0].engine.global_addr)[0]

        local_addr = desc.engine.get_tensor_addr(size_bytes)
        return desc.global_base + local_addr

    @staticmethod
    def _aligned_size(engine: "MemoryEngine", size_bytes: int) -> int:
        return engine.align_up(size_bytes)

    # ------------------------------------------------------------------
    # Event interface
    # ------------------------------------------------------------------

    def submit(
        self, event: Event,
    ) -> List[Tuple[str, float, "MemoryRequestMetrics"]]:
        """Submit one arrival event; return [(rid, finish_time, metrics), ...].

        Constructs a MemoryAccess from the event and forwards to the
        resolved engine.  Pure routing — no event-queue logic.
        """
        access = MemoryAccess(
            request_id=event.request_id,
            source_id=event.source_id,
            addr=event.addr,
            size_bytes=event.size_bytes,
            req_type=event.req_type,
            mem_engine_id=event.mem_engine_id,
        )
        engine = self._validate_access(access)
        local_addr = access.addr - engine.global_base
        return engine.submit(access, local_addr=local_addr, now=event.time)

    def _validate_access(self, access: MemoryAccess) -> "MemoryEngine":
        """Validate the access and return its owning engine."""
        engine = self.resolve_engine(access.addr, access.size_bytes)
        if (
            access.mem_engine_id is not None
            and access.mem_engine_id != engine.instance_id
        ):
            raise ValueError(
                f"access addr 0x{access.addr:x} belongs to engine "
                f"{engine.instance_id}, but mem_engine_id "
                f"{access.mem_engine_id} was specified"
            )
        return engine

def _make_engine(config: "MemoryEngineConfig") -> "MemoryEngine":
    """Create a MemoryEngine, importing lazily to avoid import cycles."""
    from ..memory_engine import MemoryEngine
    return MemoryEngine(config)
