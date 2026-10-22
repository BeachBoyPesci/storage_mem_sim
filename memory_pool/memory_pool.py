"""MemoryPool — 多实例地址管理、路由与池级指标。"""

import bisect
import dataclasses
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from ..memory_engine import MemoryEngineConfig
from ..memory_metrics import MemoryMetrics
from ..memory_type import MemoryRequestType, MemoryType

if TYPE_CHECKING:
    from ..memory_engine import MemoryEngine

logger = logging.getLogger(__name__)


@dataclass
class MemoryPoolConfig:
    """MemoryPool 的配置，多实例场景的统一配置入口。

    Attributes:
        instance_count: 实例数量（>= 1）。
        memory_type: 介质类型元数据（透传给各 engine，不影响仿真行为）。
        media_config: 介质后端配置。其 ``capacity`` 按**总容量**（GiB）
                      解释；``__post_init__`` 会将其均分为每实例容量
                      （capacity ÷ instance_count）后写回。``bandwidth``
                      始终是每实例全量带宽，不均分。
    """

    instance_count: int = 1
    memory_type: MemoryType = MemoryType.HBM
    media_config: Optional[object] = None   # MediaConfig，使用前必须设置

    def __post_init__(self):
        if self.instance_count < 1:
            raise ValueError(
                f"instance_count must be >= 1, got {self.instance_count}"
            )
        if self.media_config is not None:
            # 总容量口径 → 每实例容量：各 engine 拿到的 media_config
            # 已经是除完的值。
            total_gib = self.media_config.capacity
            per_gib = total_gib / self.instance_count
            self.media_config = dataclasses.replace(
                self.media_config, capacity=per_gib
            )
            if self.instance_count > 1:
                logger.info(
                    "MemoryPoolConfig: capacity=%.1f GB is TOTAL capacity; "
                    "per-instance = %.1f / %d = %.1f GiB",
                    total_gib, total_gib, self.instance_count, per_gib,
                )


class MemoryPool:
    """由多个 MemoryEngine 实例组成、共享全局地址空间的池。

    池拥有全局字节地址空间：每个实例获得一个固定且不重叠的窗口
    ``[global_base, global_base + capacity)``，窗口信息（基址、容量）
    直接保存在 engine 上（``global_base`` / ``capacity_bytes``）。所有
    池级地址均为全局地址；实例本地地址通过减去窗口基址得到。

    请求总是发往其数据所在放置的实例（分配期放置，而非逐请求均衡）。
    不支持跨实例的条带化、迁移或复制。

    池通过 ``issue_request()`` 接收成批请求，按地址窗口把每个请求路由到
    所属的 engine，并按并行语义聚合各 engine 的结果：池级 ``time`` 取
    各实例时间的最大值，池级 ``bandwidth`` 为总字节数除以该最大时间。
    """

    def __init__(self, config: MemoryPoolConfig):
        """按 *config* 构建由同构 engine 组成的池。

        每个实例的 ``MemoryEngineConfig`` 由 pool 内部从
        ``config.memory_type`` / ``config.media_config`` 派生。

        TODO(phase-2): 支持可插拔的分配策略
        （LEAST_ALLOCATED、加权等）。当前固定为 ROUND_ROBIN。

        TODO(phase-2): 支持异构实例（各 engine 容量/带宽不同）。
        """
        engine_config = MemoryEngineConfig(
            memory_type=config.memory_type,
            media_config=config.media_config,
        )

        # 构建各 engine，窗口基址直接记录在 engine.global_base 上。
        self._engines: List["MemoryEngine"] = []
        base = 0
        for idx in range(config.instance_count):
            cfg = dataclasses.replace(engine_config) if idx > 0 else engine_config
            from ..memory_engine import MemoryEngine
            engine = MemoryEngine(cfg)
            engine.instance_id = idx
            engine.global_base = base
            self._engines.append(engine)
            base += engine.capacity_bytes

        self._bases = [e.global_base for e in self._engines]
        self._rr_counter = 0

    # ------------------------------------------------------------------
    # 访问器
    # ------------------------------------------------------------------

    @property
    def instances(self) -> tuple:
        """池中全部 MemoryEngine 实例组成的元组。"""
        return tuple(self._engines)

    def get_engine(self, engine_id: int) -> "MemoryEngine":
        """返回给定实例 id 对应的 engine。"""
        if not 0 <= engine_id < len(self._engines):
            raise IndexError(
                f"engine_id {engine_id} out of range for "
                f"{len(self._engines)} instances"
            )
        return self._engines[engine_id]

    def resolve_engine(self, addr: int, size_bytes: int) -> "MemoryEngine":
        """把一段全局地址区间解析为拥有它的 engine。"""
        if addr < 0:
            raise ValueError(f"addr must be >= 0, got {addr}")
        if size_bytes <= 0:
            raise ValueError(f"size_bytes must be > 0, got {size_bytes}")
        idx = bisect.bisect_right(self._bases, addr) - 1
        if idx < 0:
            raise ValueError(f"addr {addr} is below the first window (base=0)")
        engine = self._engines[idx]
        end = addr + size_bytes
        if end > engine.global_base + engine.capacity_bytes:
            raise ValueError(
                f"request [0x{addr:x}, 0x{end:x}) spans beyond instance "
                f"{idx} window [0x{engine.global_base:x}, "
                f"0x{engine.global_base + engine.capacity_bytes:x}); "
                "a request must fit entirely inside one instance window"
            )
        return engine

    # ------------------------------------------------------------------
    # 分配
    # ------------------------------------------------------------------

    def get_tensor_addr(self, size_bytes: int) -> int:
        """分配 *size_bytes* 并返回全局起始地址。

        Round-robin：从上次选中的 engine 开始向后扫描，
        选中第一个剩余容量足够的 engine。所属 engine 可后续通过
        ``resolve_engine`` 从地址反查。
        """
        if size_bytes <= 0:
            raise ValueError(f"size_bytes must be > 0, got {size_bytes}")

        # Round-robin：从 _rr_counter 开始扫描，选中第一个放得下的实例。
        # TODO(phase-2): 可插拔分配策略。
        n = len(self._engines)
        start = self._rr_counter % n
        for offset in range(n):
            idx = (start + offset) % n
            engine = self._engines[idx]
            aligned = self._aligned_size(engine, size_bytes)
            if engine.remaining_capacity_bytes >= aligned:
                self._rr_counter = (idx + 1) % n
                local_addr = engine.get_tensor_addr(size_bytes)
                return engine.global_base + local_addr

        detail = ", ".join(
            f"engine {i}: capacity={e.capacity_bytes}, "
            f"remaining={e.remaining_capacity_bytes}"
            for i, e in enumerate(self._engines)
        )
        raise ValueError(
            f"no instance has remaining capacity >= aligned {size_bytes} B; "
            f"[{detail}]"
        )

    @staticmethod
    def _aligned_size(engine: "MemoryEngine", size_bytes: int) -> int:
        return engine.align_up(size_bytes)

    # ------------------------------------------------------------------
    # 同步批量路径
    # ------------------------------------------------------------------

    def issue_request(
        self,
        addr: List[int],
        size: List[int],
        req_type: List[MemoryRequestType],
    ) -> MemoryMetrics:
        """通过池同步执行一批请求。

        三个列表必须等长，且全部使用**全局**地址。每个请求按其地址窗口
        路由到所属的 engine（见 ``resolve_engine``）；发给各 engine 的
        子批次使用本地地址。假定各实例并行执行，因此池级结果按如下方式
        聚合：``total_time``/``cycles`` 取各实例最大值，
        ``memory_reqs_num`` 求和，``bandwidth`` = 总字节数 / 最大时间
        （精确重算），``iops`` 为各实例非 None 值之和（所有实例都为
        None 时为 None）。
        """
        n = len(addr)
        if len(size) != n or len(req_type) != n:
            raise ValueError("addr, size, req_type must have the same length")
        if n == 0:
            return MemoryMetrics()

        per_engine: Dict[
            int, Tuple["MemoryEngine", List[int], List[int], List[MemoryRequestType]]
        ] = {}
        for i in range(n):
            engine = self.resolve_engine(addr[i], size[i])
            local = addr[i] - engine.global_base
            entry = per_engine.setdefault(
                engine.instance_id, (engine, [], [], [])
            )
            entry[1].append(local)
            entry[2].append(size[i])
            entry[3].append(req_type[i])

        total_time = 0.0
        total_cycles = 0
        total_reqs = 0
        total_bytes = 0
        iops_values: List[float] = []
        iops_read_values: List[float] = []
        iops_write_values: List[float] = []
        for engine, local_addrs, sizes, types in per_engine.values():
            m = engine.issue_request(local_addrs, sizes, types)
            total_time = max(total_time, m.total_time)
            total_cycles = max(total_cycles, m.cycles)
            total_reqs += m.memory_reqs_num
            total_bytes += sum(sizes)
            if m.iops is not None:
                iops_values.append(m.iops)
            if m.iops_read is not None:
                iops_read_values.append(m.iops_read)
            if m.iops_write is not None:
                iops_write_values.append(m.iops_write)

        return MemoryMetrics(
            cycles=total_cycles,
            total_time=total_time,
            memory_reqs_num=total_reqs,
            global_memory_reqs_num=total_reqs,
            bandwidth=(total_bytes / total_time if total_time > 0 else 0.0),
            iops=sum(iops_values) if iops_values else None,
            iops_read=sum(iops_read_values) if iops_read_values else None,
            iops_write=sum(iops_write_values) if iops_write_values else None,
        )
