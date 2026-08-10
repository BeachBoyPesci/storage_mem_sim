"""Memory engine configuration.

Defines the configuration parameters for the MemoryEngine.
Capacity values are auto-derived from media_config.capacity.
"""

import warnings
from dataclasses import dataclass, field
from typing import Optional

from .memory_type import MemoryType


@dataclass
class MemoryEngineConfig:
    """Configuration for MemoryEngine.

    Attributes:
        memory_type: Type of memory (HBM, DDR, SSD).
        media_config: Media backend configuration. The MemoryEngine uses
                      MediaSystemFactory.create(media_config) internally.
                      Must set media_config.capacity (GB).
        granularity: Address alignment granularity in bytes. For Ramulator,
                     auto-derived from DRAM spec; fallback for other backends.
        dp_size: Deprecated. Data-parallel replication is no longer
                 performed by the engine; only the value 1 is supported
                 without warning. Use MemoryPool / explicit workload-level
                 replication instead.
        storage_instance_num: Deprecated. Instance distribution is no
                              longer performed by the engine; only the
                              value 1 is supported without warning. Use
                              MemoryPool instead.

    Auto-computed (from media_config.capacity, single-instance semantics):
        total_capacity: Instance capacity in bytes (= capacity_GB * 1024**3).
        per_dp_capacity: Equal to total_capacity (retained for
                         compatibility).
        capacity: Equal to total_capacity.
    """
    memory_type: MemoryType = MemoryType.HBM
    media_config: Optional[object] = None   # MediaConfig, set before use
    granularity: int = field(default=0, init=False)  # set by MemoryEngine from backend
    dp_size: int = 1
    storage_instance_num: int = 1

    # Auto-computed in __post_init__ from media_config.capacity
    total_capacity: int = field(default=0, init=False)
    per_dp_capacity: int = field(default=0, init=False)
    capacity: int = field(default=0, init=False)

    def __post_init__(self):
        if self.dp_size < 1:
            raise ValueError(f"dp_size must be >= 1, got {self.dp_size}")
        if self.storage_instance_num < 1:
            raise ValueError(f"storage_instance_num must be >= 1, got {self.storage_instance_num}")
        if self.dp_size != 1:
            warnings.warn(
                f"dp_size={self.dp_size} is deprecated: MemoryEngine no "
                "longer replicates requests. Replication must be expressed "
                "by the workload layer or MemoryPool.",
                DeprecationWarning,
                stacklevel=2,
            )
        if self.storage_instance_num != 1:
            warnings.warn(
                f"storage_instance_num={self.storage_instance_num} is "
                "deprecated: MemoryEngine is a single physical instance. "
                "Use MemoryPool for multi-instance setups.",
                DeprecationWarning,
                stacklevel=2,
            )

        if self.media_config is not None:
            # Single-instance semantics: all derived capacities equal the
            # media_config capacity (no division by dp/instances anymore).
            total_capacity = int(self.media_config.capacity * 1024 ** 3)
            self.total_capacity = total_capacity
            self.per_dp_capacity = total_capacity
            self.capacity = total_capacity
