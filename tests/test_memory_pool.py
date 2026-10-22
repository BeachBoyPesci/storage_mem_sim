"""Tests for MemoryPool: global address windows, allocation, sync routing,
and pool-level metrics aggregation."""

import pytest

from ..memory_type import MemoryType, MemoryRequestType
from ..media import (
    MediaConfig,
    MediaSystemBackend,
)
from ..memory_pool import MemoryPool, MemoryPoolConfig

_GIB = 1024 ** 3


def _pool_config(instance_count=2, capacity=1.0, bandwidth=100.0):
    # MemoryPoolConfig.media_config.capacity 为总容量口径；
    # capacity 参数表示期望的每实例容量。
    return MemoryPoolConfig(
        instance_count=instance_count,
        memory_type=MemoryType.HBM,
        media_config=MediaConfig(
            media_type=MediaSystemBackend.ANALYTIC,
            capacity=capacity * instance_count,
            bandwidth=bandwidth,
        ),
    )


def _pool(instance_count=2, capacity=1.0, bandwidth=100.0, **kwargs):
    return MemoryPool(
        _pool_config(instance_count, capacity, bandwidth),
        **kwargs,
    )


class TestPoolAddressWindows:
    def test_homogeneous_windows(self):
        pool = _pool(instance_count=3, capacity=1.0)
        bases = [e.global_base for e in pool.instances]
        assert bases == [0, _GIB, 2 * _GIB]

    def test_get_tensor_addr_returns_global(self):
        pool = _pool(instance_count=2, capacity=1.0)
        addr = pool.get_tensor_addr(64)
        assert 0 <= addr < _GIB
        assert addr % 64 == 0

    def test_round_robin_placement(self):
        pool = _pool(instance_count=2, capacity=1.0)
        addrs = [pool.get_tensor_addr(64) for _ in range(4)]
        assert [addr // _GIB for addr in addrs] == [0, 1, 0, 1]

    def test_round_robin_skips_insufficient_capacity(self):
        pool = _pool(instance_count=2, capacity=1.0)
        pool.get_tensor_addr(_GIB)
        addr = pool.get_tensor_addr(64)
        assert _GIB <= addr < 2 * _GIB

    def test_window_boundary_exact_fit(self):
        pool = _pool(instance_count=2, capacity=1.0)
        addr = _GIB - 512
        engine = pool.resolve_engine(addr, 512)
        assert engine.instance_id == 0
        with pytest.raises(ValueError, match="spans beyond"):
            pool.resolve_engine(_GIB - 511, 512)

    def test_cross_window_request_rejected(self):
        pool = _pool(instance_count=2, capacity=1.0)
        with pytest.raises(ValueError, match="spans beyond"):
            pool.resolve_engine(_GIB - 64, 128)

    def test_all_instances_exhausted_raises(self):
        pool = _pool(instance_count=2, capacity=1.0)
        pool.get_tensor_addr(_GIB)
        pool.get_tensor_addr(_GIB)
        with pytest.raises(ValueError, match="no instance has remaining"):
            pool.get_tensor_addr(64)

    def test_negative_address_rejected(self):
        pool = _pool(instance_count=1, capacity=1.0)
        with pytest.raises(ValueError, match="addr must be >= 0"):
            pool.resolve_engine(-1, 64)

    def test_global_local_conversion_roundtrip(self):
        pool = _pool(instance_count=3, capacity=1.0)
        addr = pool.get_tensor_addr(64)
        engine = pool.resolve_engine(addr, 64)
        local = addr - engine.global_base
        assert engine.instance_id == addr // _GIB
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

    def test_cross_window_issue_rejected(self):
        pool = _pool(instance_count=2, capacity=1.0)
        with pytest.raises(ValueError, match="spans beyond"):
            pool.issue_request(
                [_GIB - 32], [64], [MemoryRequestType.KREAD]
            )

    def test_analytic_iops_is_none(self):
        pool = _pool(instance_count=2, capacity=1.0)
        metrics = pool.issue_request(
            [0, _GIB], [64, 64],
            [MemoryRequestType.KREAD, MemoryRequestType.KREAD],
        )
        assert metrics.iops is None


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

    def test_config_divides_total_capacity(self):
        """media_config.capacity is TOTAL; each engine gets an equal share."""
        config = MemoryPoolConfig(
            instance_count=4,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                capacity=8.0,
                bandwidth=100.0,
            ),
        )
        assert config.media_config.capacity == 2.0
        pool = MemoryPool(config)
        for e in pool.instances:
            assert e.capacity_bytes == 2 * _GIB

    def test_pool_config_defaults(self):
        config = MemoryPoolConfig(instance_count=2)
        assert config.instance_count == 2
