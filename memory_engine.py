"""Memory Engine — core module for memory request lifecycle management.

MemoryEngine sits between the service/model layer and the media simulation
layer. It handles address allocation, request construction, request
transformation (multi-DP / multi-storage-instance), and delegates
performance simulation to the configured MediaSystem backend.
"""

import math
import logging
from typing import List

from .memory_type import MemoryRequestType
from .memory_config import MemoryEngineConfig
from .memory_object import MemoryObject
from .memory_request import MemoryRequest
from .memory_metrics import MemoryMetrics, MemoryEngineMetrics

logger = logging.getLogger(__name__)


class MemoryEngine:
    """Main memory engine managing address allocation and request dispatch.

    The engine maintains a monotonically increasing global address counter
    across all DP ranks and storage instances. Each call to issue_request
    decomposes high-level memory operations into media-level requests and
    delegates to the configured MediaSystem for performance simulation.

    Usage:
        engine_config = MemoryEngineConfig(
            memory_type=MemoryType.HBM,
            media_config=media_config,
            ...
        )
        engine = MemoryEngine(engine_config)

        addr = engine.get_tensor_addr(64 * 1024 * 1024)  # 64MB tensor
        metrics = engine.issue_request([addr], [size], [MemoryRequestType.KREAD])
    """

    def __init__(self, mem_config: MemoryEngineConfig):
        """Initialize the memory engine.

        Creates the MediaSystem internally via MediaSystemFactory
        using mem_config.media_config.

        Args:
            mem_config: Configuration for address space, granularity,
                        DP, and media backend.
        """
        self.mem_config = mem_config
        self.global_addr: int = 0
        self.engine_metrics = MemoryEngineMetrics()

        if mem_config.media_config is None:
            raise ValueError("MemoryEngineConfig.media_config is required")
        from .media.media_system_factory import MediaSystemFactory
        self.media_system = MediaSystemFactory.create(mem_config.media_config)

        # Set granularity from backend: Ramulator uses _tx_bytes (e.g. 32),
        # Analytic has no decomposition — use 64 (standard cache line).
        tx = getattr(self.media_system, '_tx_bytes', None)
        self.mem_config.granularity = tx if tx else 64

        logger.info(
            "MemoryEngine init: mem_type=%s granularity=%d dp=%d instances=%d "
            "capacity=%dGB per_dp=%dGB",
            mem_config.memory_type.value, self.mem_config.granularity,
            mem_config.dp_size, mem_config.storage_instance_num,
            mem_config.total_capacity // (1024 ** 3),
            mem_config.per_dp_capacity // (1024 ** 3))

    def align_up(self, size: int) -> int:
        """Align size up to the configured granularity.

        Args:
            size: Raw size in bytes.

        Returns:
            Aligned size (ceiling to granularity boundary).
        """
        step = self.mem_config.granularity
        return math.ceil(size / step) * step

    def get_tensor_addr(self, size: int) -> int:
        """Allocate an aligned address for a tensor of the given size.

        The global address counter is advanced by the aligned size. Raises
        OverflowError if the allocation would exceed per_dp_capacity.

        Args:
            size: Tensor size in bytes (will be aligned up).

        Returns:
            The starting address for this tensor allocation.

        Raises:
            OverflowError: If the allocation exceeds per_dp_capacity.
        """
        aligned_size = self.align_up(size)
        tensor_addr = self.global_addr
        self.global_addr += aligned_size

        if self.mem_config.per_dp_capacity > 0 and self.global_addr > self.mem_config.per_dp_capacity:
            logger.warning(
                "Address overflow: size=%d aligned=%d global_addr=%d "
                "per_dp_capacity=%d",
                size, aligned_size, self.global_addr,
                self.mem_config.per_dp_capacity)
            raise OverflowError(
                f"Address overflow: global_addr {self.global_addr} exceeds "
                f"per_dp_capacity {self.mem_config.per_dp_capacity}"
            )

        return tensor_addr

    def reset_addr(self):
        """Reset the global address counter to zero."""
        self.global_addr = 0

    def create_request(
        self, addr: int, size: int, req_type: MemoryRequestType
    ) -> MemoryRequest:
        """Create a MemoryRequest from the given parameters.

        Constructs a MemoryObject then wraps it in a MemoryRequest.

        Args:
            addr: Base address of the access.
            size: Size of the access in bytes.
            req_type: Type of request (KREAD or KWRITE).

        Returns:
            A MemoryRequest holding the constructed MemoryObject.
        """
        memory_object = MemoryObject(addr, size, req_type, self.mem_config)
        return MemoryRequest(memory_object=memory_object)

    def issue_request(
        self,
        addr: List[int],
        size: List[int],
        req_type: List[MemoryRequestType],
    ) -> MemoryMetrics:

        if self.media_system is None:
            raise RuntimeError(
                "No media_system configured. "
                "Set media_config in MemoryEngineConfig."
            )

        n = len(addr)
        if len(size) != n or len(req_type) != n:
            raise ValueError(
                f"addr, size, req_type must have the same length "
                f"(got {n}, {len(size)}, {len(req_type)})"
            )

        dp_size = self.mem_config.dp_size
        instance_num = self.mem_config.storage_instance_num
        total_bytes = sum(size)

        logger.debug(
            "issue_request: n=%d dp=%d instances=%d addr_range=[0x%x, 0x%x] "
            "total_bytes=%d",
            n, dp_size, instance_num,
            min(addr) if addr else 0, max(addr) if addr else 0,
            total_bytes)

        for i in range(n):
            if addr[i] < 0:
                raise ValueError(f"addr[{i}] must be >= 0, got {addr[i]}")
            if size[i] < 0:
                raise ValueError(f"size[{i}] must be >= 0, got {size[i]}")
            if size[i] == 0:
                raise ValueError(f"size[{i}] must be > 0")

        # Build request list with DP/instance transformation
        mem_reqs: List[MemoryRequest] = []

        for i in range(n):
            for dp_rank in range(dp_size):
                # Each DP rank has an independent address offset within a
                # per-rank partition of the per_dp_capacity.
                rank_offset = dp_rank * self.mem_config.per_dp_capacity
                effective_addr = addr[i] + rank_offset
                mem_req = self.create_request(effective_addr, size[i], req_type[i])
                mem_reqs.append(mem_req)

        # Distribute across storage instances (round-robin).
        # Multiple instances are parallel — only simulate the first
        # non-empty instance. num_media_reqs comes from the media system
        # (reflects the actual decomposition granularity of the backend).
        if len(mem_reqs) == 0:
            return MemoryMetrics()

        if instance_num > 1:
            instance_reqs: List[List[MemoryRequest]] = [
                [] for _ in range(instance_num)
            ]
            for idx, mem_req in enumerate(mem_reqs):
                instance_reqs[idx % instance_num].append(mem_req)

            total_media_metrics = None
            simulated_bytes = 0
            for inst_reqs in instance_reqs:
                if inst_reqs:
                    total_media_metrics = self.media_system.handler_mem_request(inst_reqs)
                    simulated_bytes = sum(
                        req.memory_object.size for req in inst_reqs
                    )
                    break
        else:
            total_media_metrics = self.media_system.handler_mem_request(mem_reqs)
            simulated_bytes = sum(
                req.memory_object.size for req in mem_reqs
            )

        # Engine-level request counts:
        #   memory_reqs_num        = requests in the simulated instance
        #   global_memory_reqs_num = total across all DP × instances
        sim_req_count = len(mem_reqs)
        if instance_num > 1:
            for inst_reqs in instance_reqs:
                if inst_reqs:
                    sim_req_count = len(inst_reqs)
                    break

        mem_metrics = MemoryMetrics(
            cycles=total_media_metrics.cycles,
            total_time=total_media_metrics.time,
            memory_reqs_num=sim_req_count,
            global_memory_reqs_num=len(mem_reqs),
            bandwidth=total_media_metrics.bandwidth,
            iops=total_media_metrics.iops,
            iops_read=total_media_metrics.iops_read,
            iops_write=total_media_metrics.iops_write,
        )

        self.engine_metrics.update(mem_metrics, simulated_bytes)

        return mem_metrics

    def get_engine_metrics(self) -> MemoryEngineMetrics:
        """Return the cumulative engine metrics.

        Returns:
            The MemoryEngineMetrics with accumulated history.
        """
        return self.engine_metrics

    def reset_engine_metrics(self):
        """Reset cumulative engine metrics."""
        self.engine_metrics = MemoryEngineMetrics()
