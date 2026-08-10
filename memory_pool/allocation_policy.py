"""AllocationPolicy — how pool allocations are placed on instances."""

from enum import Enum


class AllocationPolicy(Enum):
    """Instance placement policy for pool-level tensor allocation.

    The policy compares *allocated capacity*, not instantaneous active
    bandwidth, so data placement is reproducible and independent of the
    access timing.
    """

    ROUND_ROBIN = "round_robin"
    LEAST_ALLOCATED = "least_allocated"
