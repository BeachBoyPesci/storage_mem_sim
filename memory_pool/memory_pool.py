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
        media_config: 介质后端配置。``capacity`` 按**总容量**（GiB）
                      解释：``MemoryPool`` 构建时按整 GiB 整除，前
                      n-1 个实例各 ⌊total ÷ n⌋ GiB，余数全部归最后
                      一个实例；第一个实例容量小于地址对齐粒度
                      （整 GiB 整除下即份额不足 1 GiB）时抛
                      ``ValueError``。``bandwidth`` 也按
                      **总量**口径解释：``__post_init__`` 将其均分为
                      每实例值（bandwidth ÷ instance_count）后写回，
                      聚合峰值 = 配置带宽；均分只影响 Analytic 后端
                      （Ramulator/MQSim 的带宽由器件组织/仿真决定，
                      不消费该字段）。
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
            # 总带宽口径 → 每实例值：聚合峰值 = 配置带宽（N 实例各得
            # bandwidth/N，并行执行）。capacity 保持总容量口径，窗口
            # 划分（整 GiB 整除、余数归最后实例）在 MemoryPool 构建时
            # 进行。
            total_bw = self.media_config.bandwidth
            per_bw = total_bw / self.instance_count
            self.media_config = dataclasses.replace(
                self.media_config, bandwidth=per_bw
            )
            if self.instance_count > 1:
                logger.info(
                    "MemoryPoolConfig: bandwidth=%.1f GiB/s is TOTAL; "
                    "per-instance = %.1f / %d = %.1f GiB/s",
                    total_bw, total_bw, self.instance_count, per_bw,
                )


class MemoryPool:
    """由多个 MemoryEngine 实例组成、共享全局地址空间的池。

    池拥有全局字节地址空间：每个实例获得一个固定且不重叠的窗口
    ``[global_base, global_base + capacity)``，窗口信息（基址、容量）
    直接保存在 engine 上（``global_base`` / ``capacity_bytes``）。所有
    池级地址均为全局地址；实例本地地址通过减去窗口基址得到。

    分配为**顺序全局打包**：水位单调推进，填满一个窗口后自然延伸到
    下一个窗口，因此一个 allocation 可以跨越多个实例（engine 本地水位
    与全局水位天然同步）。请求同样可以跨界：``issue_request()`` 按窗口
    边界把跨界请求切成片段，分别路由到所属 engine。不支持迁移和复制。

    池按并行语义聚合各 engine 的结果：池级 ``time`` 取各实例时间的
    最大值，池级 ``bandwidth`` 为总字节数除以该最大时间。
    """

    def __init__(self, config: MemoryPoolConfig):
        """按 *config* 构建 engine 池。

        每个实例的 ``MemoryEngineConfig`` 由 pool 内部从
        ``config.memory_type`` / ``config.media_config`` 派生。窗口
        容量按整 GiB 划分：前 n-1 个实例各 ⌊总容量 ÷ n⌋ GiB，余数
        全部归最后一个实例；除容量余数外各实例同构。

        TODO(phase-2): 支持可插拔的分配策略（LEAST_ALLOCATED、加权、
        段表条带化等）。当前固定为顺序全局打包。

        TODO(phase-2): 支持异构实例（各 engine 容量/带宽不同）。
        """
        engine_config = MemoryEngineConfig(
            memory_type=config.memory_type,
            media_config=config.media_config,
        )

        n = config.instance_count
        total_gib = (
            config.media_config.capacity if config.media_config else 0.0
        )
        base_gib = int(total_gib / n)
        last_gib = total_gib - base_gib * (n - 1)
        if n > 1 and last_gib != base_gib:
            logger.info(
                "MemoryPool: %.4f GiB over %d instances: base windows "
                "%d GiB, last window takes the remainder (%.4f GiB)",
                total_gib, n, base_gib, last_gib,
            )

        # 构建各 engine，窗口基址直接记录在 engine.global_base 上。
        # 整 GiB 窗口边界必然按地址粒度对齐，打包不变量不受影响。
        self._engines: List["MemoryEngine"] = []
        base = 0
        for idx in range(n):
            cfg = dataclasses.replace(engine_config)
            cfg.capacity = int(
                (last_gib if idx == n - 1 else base_gib) * 1024 ** 3
            )
            from ..memory_engine import MemoryEngine
            engine = MemoryEngine(cfg)
            if idx == 0:
                # 第一个窗口至少要容得下一个粒度单位：整 GiB 整除下即
                # base_gib == 0（份额不足 1 GiB）；单实例池也可能配置
                # 出亚粒度容量。
                gran = engine.mem_config.granularity
                if engine.capacity_bytes < gran:
                    raise ValueError(
                        f"第一个 engine 的容量 {engine.capacity_bytes} B "
                        f"小于地址对齐粒度 {gran} B（总容量 "
                        f"{int(total_gib * 1024 ** 3)} B ÷ {n} 个实例）；"
                        f"请增大 capacity 或减少 instances"
                    )
            engine.instance_id = idx
            engine.global_base = base
            self._engines.append(engine)
            base += engine.capacity_bytes

        self._bases = [e.global_base for e in self._engines]
        self._total_capacity = base

    # ------------------------------------------------------------------
    # 访问器
    # ------------------------------------------------------------------

    @property
    def instances(self) -> tuple:
        """池中全部 MemoryEngine 实例组成的元组。"""
        return tuple(self._engines)

    @property
    def total_capacity_bytes(self) -> int:
        """池的总容量（字节），即各实例窗口容量之和。"""
        return self._total_capacity

    @property
    def global_addr(self) -> int:
        """下一次分配的起始全局地址（分配水位）。

        顺序全局打包下，水位 = 各 engine 本地水位之和（前面的实例
        填满后才轮到后面的实例），对任意实例数都有确定语义。
        """
        return sum(e.global_addr for e in self._engines)

    def get_engine(self, engine_id: int) -> "MemoryEngine":
        """返回给定实例 id 对应的 engine。"""
        if not 0 <= engine_id < len(self._engines):
            raise IndexError(
                f"engine_id {engine_id} out of range for "
                f"{len(self._engines)} instances"
            )
        return self._engines[engine_id]

    def _split_range(self, addr: int, size_bytes: int):
        """把全局区间按窗口边界切成 ``(engine, local_addr, frag_bytes)``。

        单窗口区间只产生一个片段；跨界区间产生多个片段，按地址顺序。
        """
        if addr < 0:
            raise ValueError(f"addr must be >= 0, got {addr}")
        if size_bytes <= 0:
            raise ValueError(f"size_bytes must be > 0, got {size_bytes}")
        end = addr + size_bytes
        if end > self._total_capacity:
            raise ValueError(
                f"range [0x{addr:x}, 0x{end:x}) exceeds pool capacity "
                f"0x{self._total_capacity:x}"
            )
        idx = bisect.bisect_right(self._bases, addr) - 1
        pos = addr
        while pos < end:
            engine = self._engines[idx]
            window_end = engine.global_base + engine.capacity_bytes
            frag = min(end, window_end) - pos
            yield engine, pos - engine.global_base, frag
            pos += frag
            idx += 1

    # ------------------------------------------------------------------
    # 分配
    # ------------------------------------------------------------------

    def get_tensor_addr(self, size_bytes: int) -> int:
        """分配 *size_bytes* 并返回全局起始地址。

        顺序全局打包：从当前水位 ``global_addr`` 取一段连续全局区间，
        溢出窗口边界的部分自然延伸到后续实例（各 engine 的本地水位
        与全局水位保持同步）。
        """
        if size_bytes <= 0:
            raise ValueError(f"size_bytes must be > 0, got {size_bytes}")

        aligned = self.align_up(size_bytes)
        addr = self.global_addr
        if addr + aligned > self._total_capacity:
            raise ValueError(
                f"pool capacity exhausted: watermark 0x{addr:x} + aligned "
                f"{aligned} B exceeds total 0x{self._total_capacity:x} B"
            )

        # 逐窗口分配片段。打包不变量保证 engine 返回的本地地址与窗口
        # 偏移一致；不一致说明有调用方绕开 pool 直接分配过。
        for engine, local, frag in self._split_range(addr, aligned):
            got = engine.get_tensor_addr(frag)
            if got != local:
                raise RuntimeError(
                    f"allocation invariant broken: engine "
                    f"{engine.instance_id} watermark 0x{got:x} != expected "
                    f"local 0x{local:x}; do not allocate on pool engines "
                    "directly"
                )
        return addr

    def align_up(self, size_bytes: int) -> int:
        """按对齐粒度向上取整。

        各实例同构、粒度一致，直接委托给第一个 engine 的同名方法。
        """
        return self._engines[0].align_up(size_bytes)

    def reset_addr(self):
        """将所有成员 engine 的分配水位清零。

        池的全局水位是各 engine 本地水位之和，没有独立变量，因此池级
        重置必须逐个清零成员 engine。这是唯一合法的池级重置方式：
        单独对某个成员 engine 调 ``reset_addr()`` 会破坏打包不变量，
        导致静默地址复用或下一次分配时 invariant 报错。
        """
        for engine in self._engines:
            engine.reset_addr()

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

        三个列表必须等长，且全部使用**全局**地址。每个请求按窗口边界
        切成一个或多个片段（见 ``_split_range``），跨界请求的各片段
        分别路由到所属 engine；发给各 engine 的子批次使用本地地址。
        假定各实例并行执行，因此池级结果按如下方式
        聚合：``total_time``/``cycles`` 取各实例最大值，
        ``memory_reqs_num`` 求和，``bandwidth`` = 总字节数 / 最大时间
        （精确重算）；IOPS 是单实例设备属性，不求和——``iops`` 为各
        实例非 None 值的平均值（全 None 时为 None，read/write 通道
        同理）。
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
            for engine, local, frag in self._split_range(addr[i], size[i]):
                entry = per_engine.setdefault(
                    engine.instance_id, (engine, [], [], [])
                )
                entry[1].append(local)
                entry[2].append(frag)
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

        def _mean(values: List[float]) -> Optional[float]:
            if not values:
                return None
            return sum(values) / len(values)

        return MemoryMetrics(
            cycles=total_cycles,
            total_time=total_time,
            memory_reqs_num=total_reqs,
            bandwidth=(total_bytes / total_time if total_time > 0 else 0.0),
            iops=_mean(iops_values),
            iops_read=_mean(iops_read_values),
            iops_write=_mean(iops_write_values),
        )
