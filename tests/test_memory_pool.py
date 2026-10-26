"""Tests for MemoryPool: global address windows, allocation, sync routing,
and pool-level metrics aggregation."""

import logging

import pytest

from ..memory_type import MemoryType, MemoryRequestType
from ..media import (
    MediaConfig,
    MediaMetrics,
    MediaSystemBackend,
)
from ..memory_pool import MemoryPool, MemoryPoolConfig

_GIB = 1024 ** 3


def _pool_config(instance_count=2, capacity=1.0, bandwidth=100.0):
    # MemoryPoolConfig.media_config 的 capacity 与 bandwidth 均为总量
    # 口径；两个参数表示期望的每实例容量/带宽，构造时乘回总量。
    return MemoryPoolConfig(
        instance_count=instance_count,
        memory_type=MemoryType.HBM,
        media_config=MediaConfig(
            media_type=MediaSystemBackend.ANALYTIC,
            capacity=capacity * instance_count,
            bandwidth=bandwidth * instance_count,
        ),
    )


def _pool(instance_count=2, capacity=1.0, bandwidth=100.0):
    return MemoryPool(_pool_config(instance_count, capacity, bandwidth))


class TestPoolAddressWindows:
    def test_homogeneous_windows(self):
        pool = _pool(instance_count=3, capacity=1.0)
        bases = [e.global_base for e in pool.instances]
        assert bases == [0, _GIB, 2 * _GIB]

    def test_remainder_goes_to_last_instance(self, caplog):
        """4 GiB / 3：整 GiB 整除，前两个各 1 GiB，第三个拿余数 2 GiB。"""
        config = MemoryPoolConfig(
            instance_count=3,
            memory_type=MemoryType.HBM,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                capacity=4.0,
                bandwidth=100.0,
            ),
        )
        with caplog.at_level(logging.INFO):
            pool = MemoryPool(config)

        caps = [e.capacity_bytes for e in pool.instances]
        assert caps == [_GIB, _GIB, 2 * _GIB]
        assert pool.total_capacity_bytes == 4 * _GIB
        assert any("remainder" in r.message for r in caplog.records)
        # 全部容量（含最后一个窗口的余数）都可分配
        pool.get_tensor_addr(4 * _GIB)
        assert pool.global_addr == 4 * _GIB

    def test_sub_gib_per_instance_share_rejected(self):
        """整 GiB 整除：第一个实例份额不足 1 GiB（容量 < 粒度）时报错。"""
        config = MemoryPoolConfig(
            instance_count=2,
            memory_type=MemoryType.HBM,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                capacity=1.5,
                bandwidth=100.0,
            ),
        )
        with pytest.raises(ValueError, match="第一个 engine"):
            MemoryPool(config)

    def test_sub_granularity_first_window_rejected(self):
        """单实例池容量小于地址粒度（64 B）时同样报错。"""
        config = MemoryPoolConfig(
            instance_count=1,
            memory_type=MemoryType.HBM,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                capacity=32 / _GIB,  # 32 B < 64 B 粒度
                bandwidth=100.0,
            ),
        )
        with pytest.raises(ValueError, match="第一个 engine"):
            MemoryPool(config)

    def test_reset_addr_clears_all_engines(self):
        """池级 reset 把全部成员 engine 的水位清零，之后可从 0 重新分配。"""
        pool = _pool(instance_count=2, capacity=1.0)
        pool.get_tensor_addr(_GIB + 64)  # 跨窗口：engine0 满 + engine1 64 B
        assert pool.global_addr == _GIB + 64

        pool.reset_addr()

        assert pool.global_addr == 0
        assert all(e.global_addr == 0 for e in pool.instances)
        assert pool.get_tensor_addr(64) == 0

    def test_get_tensor_addr_returns_global(self):
        pool = _pool(instance_count=2, capacity=1.0)
        addr = pool.get_tensor_addr(64)
        assert 0 <= addr < _GIB
        assert addr % 64 == 0

    def test_sequential_packing_placement(self):
        """Allocations pack into window 0 until it fills, then spill over."""
        pool = _pool(instance_count=2, capacity=1.0)
        addrs = [pool.get_tensor_addr(64) for _ in range(4)]
        assert addrs == [0, 64, 128, 192]
        assert pool.global_addr == 256

    def test_packing_spills_into_next_window(self):
        pool = _pool(instance_count=2, capacity=1.0)
        pool.get_tensor_addr(_GIB)
        addr = pool.get_tensor_addr(64)
        assert addr == _GIB

    def test_allocation_spans_windows(self):
        """An allocation crossing the boundary advances both engines."""
        pool = _pool(instance_count=2, capacity=1.0)
        pool.get_tensor_addr(_GIB - 64)
        addr = pool.get_tensor_addr(128)
        assert addr == _GIB - 64
        e0, e1 = pool.get_engine(0), pool.get_engine(1)
        assert e0.global_addr == _GIB
        assert e1.global_addr == 64
        assert pool.global_addr == _GIB + 64

    def test_window_boundary_exact_fit(self):
        """A request ending exactly at the boundary stays on one engine."""
        pool = _pool(instance_count=2, capacity=1.0)
        pool.issue_request([_GIB - 512], [512], [MemoryRequestType.KREAD])
        assert pool.get_engine(0).get_engine_metrics().total_bytes == 512
        assert pool.get_engine(1).get_engine_metrics().total_bytes == 0

    def test_pool_capacity_exhausted_raises(self):
        pool = _pool(instance_count=2, capacity=1.0)
        pool.get_tensor_addr(2 * _GIB)
        with pytest.raises(ValueError, match="pool capacity exhausted"):
            pool.get_tensor_addr(64)

    def test_range_beyond_pool_rejected(self):
        pool = _pool(instance_count=2, capacity=1.0)
        with pytest.raises(ValueError, match="exceeds pool capacity"):
            pool.issue_request([2 * _GIB - 32], [64], [MemoryRequestType.KREAD])

    def test_negative_address_rejected(self):
        pool = _pool(instance_count=1, capacity=1.0)
        with pytest.raises(ValueError, match="addr must be >= 0"):
            pool.issue_request([-1], [64], [MemoryRequestType.KREAD])

    def test_global_local_conversion_roundtrip(self):
        pool = _pool(instance_count=3, capacity=1.0)
        addr = pool.get_tensor_addr(64)
        engine = pool.get_engine(addr // _GIB)
        local = addr - engine.global_base
        assert 0 <= local < _GIB


class TestPoolIssueRequest:
    def test_routes_by_address(self):
        """Each request lands on the engine owning its global address."""
        pool = _pool(instance_count=2, capacity=1.0)
        pool.issue_request(
            [0, _GIB],
            [1000, 2000],
            [MemoryRequestType.KREAD, MemoryRequestType.KREAD],
        )
        e0, e1 = pool.get_engine(0), pool.get_engine(1)
        assert e0.get_engine_metrics().total_bytes == 1000
        assert e1.get_engine_metrics().total_bytes == 2000

    def test_aggregation_max_time_and_summed_bytes(self):
        """Parallel instances: pool time = max, bandwidth = Σbytes/max_time."""
        pool = _pool(instance_count=2, capacity=1.0, bandwidth=100.0)
        metrics = pool.issue_request(
            [0, _GIB],
            [1000, 1000],
            [MemoryRequestType.KREAD, MemoryRequestType.KREAD],
        )
        peak = 100.0 * _GIB
        single_time = 1000 / peak
        assert metrics.total_time == pytest.approx(single_time)
        assert metrics.memory_reqs_num == 2
        assert metrics.bandwidth == pytest.approx(2000 / single_time)

    def test_total_bandwidth_divided_across_instances(self):
        """总带宽均分：2 实例总量 200 GiB/s → 单实例峰值 100 GiB/s，
        两实例并行时聚合峰值回到配置总带宽 200 GiB/s。"""
        config = MemoryPoolConfig(
            instance_count=2,
            memory_type=MemoryType.HBM,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                capacity=2.0,
                bandwidth=200.0,
            ),
        )
        pool = MemoryPool(config)
        one = pool.issue_request([0], [1000], [MemoryRequestType.KREAD])
        peak = 100.0 * _GIB
        assert one.total_time == pytest.approx(1000 / peak)

        both = pool.issue_request(
            [0, _GIB],
            [1000, 1000],
            [MemoryRequestType.KREAD, MemoryRequestType.KREAD],
        )
        assert both.total_time == pytest.approx(1000 / peak)
        assert both.bandwidth == pytest.approx(2000 * peak / 1000)

    def test_single_engine_batch_matches_direct_issue(self):
        """A batch confined to one window matches that engine's own result."""
        pool = _pool(instance_count=2, capacity=1.0, bandwidth=100.0)
        metrics = pool.issue_request(
            [0, 64],
            [64, 64],
            [MemoryRequestType.KREAD, MemoryRequestType.KWRITE],
        )
        peak = 100.0 * _GIB
        assert metrics.total_time == pytest.approx(128 / peak)
        assert metrics.memory_reqs_num == 2
        # Engine 1 was never touched.
        assert pool.get_engine(1).get_engine_metrics().total_bytes == 0

    def test_empty_input_returns_empty_metrics(self):
        pool = _pool(instance_count=2, capacity=1.0)
        metrics = pool.issue_request([], [], [])
        assert metrics.memory_reqs_num == 0
        assert metrics.total_time == 0.0

    def test_mismatched_lengths_rejected(self):
        pool = _pool(instance_count=2, capacity=1.0)
        with pytest.raises(ValueError, match="same length"):
            pool.issue_request([0], [64, 64], [MemoryRequestType.KREAD])

    def test_cross_window_issue_is_split(self):
        """A request spanning the boundary is split across both engines."""
        pool = _pool(instance_count=2, capacity=1.0, bandwidth=100.0)
        metrics = pool.issue_request(
            [_GIB - 32], [64], [MemoryRequestType.KREAD]
        )
        e0, e1 = pool.get_engine(0), pool.get_engine(1)
        assert e0.get_engine_metrics().total_bytes == 32
        assert e1.get_engine_metrics().total_bytes == 32
        assert metrics.memory_reqs_num == 2
        peak = 100.0 * _GIB
        assert metrics.total_time == pytest.approx(32 / peak)
        assert metrics.bandwidth == pytest.approx(64 / (32 / peak))

    def test_analytic_iops_is_none(self):
        pool = _pool(instance_count=2, capacity=1.0)
        metrics = pool.issue_request(
            [0, _GIB], [64, 64],
            [MemoryRequestType.KREAD, MemoryRequestType.KREAD],
        )
        assert metrics.iops is None
        assert metrics.iops_read is None
        assert metrics.iops_write is None

    def test_pool_iops_mean(self):
        """IOPS 是单实例设备属性：池级报告各实例非 None 值的平均值。"""
        class _FakeMedia:
            def __init__(self, iops):
                self._iops = iops

            def handler_mem_request(self, mem_reqs):
                return MediaMetrics(
                    time=1e-6,
                    bandwidth=1.0,
                    iops=self._iops,
                    iops_read=self._iops,
                    iops_write=self._iops,
                )

        pool = _pool(instance_count=2, capacity=1.0)
        pool.get_engine(0).media_system = _FakeMedia(1000.0)
        pool.get_engine(1).media_system = _FakeMedia(500.0)
        metrics = pool.issue_request(
            [0, _GIB], [64, 64],
            [MemoryRequestType.KREAD, MemoryRequestType.KREAD],
        )
        assert metrics.iops == 750.0
        assert metrics.iops_read == 750.0
        assert metrics.iops_write == 750.0

    def test_pool_iops_ignores_none_instances(self):
        """部分实例不报告 IOPS 时，只对非 None 的实例取平均。"""
        class _FakeMedia:
            def __init__(self, iops):
                self._iops = iops

            def handler_mem_request(self, mem_reqs):
                return MediaMetrics(
                    time=1e-6, bandwidth=1.0, iops=self._iops,
                )

        pool = _pool(instance_count=2, capacity=1.0)
        pool.get_engine(0).media_system = _FakeMedia(1000.0)
        pool.get_engine(1).media_system = _FakeMedia(None)
        metrics = pool.issue_request(
            [0, _GIB], [64, 64],
            [MemoryRequestType.KREAD, MemoryRequestType.KREAD],
        )
        assert metrics.iops == 1000.0
        assert metrics.iops_read is None


class TestPoolFactory:
    def test_engines_created(self):
        pool = _pool(instance_count=3, capacity=1.0)
        assert len(pool.instances) == 3

    def test_capacity_is_sum(self):
        pool = _pool(instance_count=4, capacity=2.0)
        total = sum(e.capacity_bytes for e in pool.instances)
        assert total == 8 * _GIB

    def test_requires_at_least_one_engine(self):
        with pytest.raises(ValueError, match="instance_count must be >= 1"):
            MemoryPoolConfig(instance_count=0)

    def test_config_keeps_total_capacity(self):
        """media_config.capacity 保持总容量口径；整 GiB 划分发生在 pool 构建时。"""
        config = MemoryPoolConfig(
            instance_count=4,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                capacity=8.0,
                bandwidth=100.0,
            ),
        )
        assert config.media_config.capacity == 8.0
        pool = MemoryPool(config)
        for e in pool.instances:
            assert e.capacity_bytes == 2 * _GIB

    def test_config_divides_total_bandwidth(self):
        """media_config.bandwidth 为总带宽口径；__post_init__ 均分写回。"""
        config = MemoryPoolConfig(
            instance_count=4,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                capacity=8.0,
                bandwidth=400.0,
            ),
        )
        assert config.media_config.bandwidth == 100.0

    def test_total_bandwidth_reaches_backend_per_instance(self):
        """均分后的带宽真正到达每个 engine 的后端（B/s 口径）。"""
        config = MemoryPoolConfig(
            instance_count=2,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                capacity=2.0,
                bandwidth=200.0,
            ),
        )
        pool = MemoryPool(config)
        for e in pool.instances:
            assert e.media_system._bandwidth_bytes_per_sec == 100.0 * _GIB

    def test_pool_config_defaults(self):
        config = MemoryPoolConfig(instance_count=2)
        assert config.instance_count == 2
