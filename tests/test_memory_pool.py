"""Tests for MemoryPool: global address windows, routing, event submit,
and pool metrics."""

import pytest

from ..memory_type import MemoryType, MemoryRequestType
from ..memory_config import MemoryEngineConfig
from ..media import (
    MediaConfig,
    MediaSystemBackend,
)
from ..memory_pool import (
    AllocationPolicy,
    Event,
    EventKind,
    MemoryPool,
    MemoryPoolConfig,
)

_GIB = 1024 ** 3


def _arrival(request_id, addr, size_bytes, source_id="s0", time=0.0):
    return Event(
        time=time, seq=0, kind=EventKind.ARRIVAL,
        source_id=source_id, request_id=request_id,
        mem_engine_id=None, addr=addr, size_bytes=size_bytes,
        req_type=MemoryRequestType.KREAD,
    )


def _engine_config(capacity=1.0, bandwidth=100.0, transport=0.0):
    return MemoryEngineConfig(
        memory_type=MemoryType.HBM,
        media_config=MediaConfig(
            media_type=MediaSystemBackend.ANALYTIC,
            capacity=capacity,
            bandwidth=bandwidth,
            transport_bandwidth=transport,
        ),
    )


def _pool(instance_count=2, capacity=1.0, bandwidth=100.0,
          transport=0.0, **kwargs):
    return MemoryPool.from_homogeneous(
        instance_count,
        _engine_config(capacity=capacity, bandwidth=bandwidth,
                       transport=transport),
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

    def test_specified_engine_allocation(self):
        pool = _pool(instance_count=2, capacity=1.0)
        addr = pool.get_tensor_addr(64, mem_engine_id=1)
        assert _GIB <= addr < 2 * _GIB

    def test_round_robin_placement(self):
        pool = _pool(instance_count=2, capacity=1.0,
                     allocation_policy=AllocationPolicy.ROUND_ROBIN)
        addrs = [pool.get_tensor_addr(64) for _ in range(4)]
        assert [addr // _GIB for addr in addrs] == [0, 1, 0, 1]

    def test_least_allocated_placement(self):
        pool = _pool(instance_count=2, capacity=1.0,
                     allocation_policy=AllocationPolicy.LEAST_ALLOCATED)
        addrs = [pool.get_tensor_addr(64) for _ in range(3)]
        assert [addr // _GIB for addr in addrs] == [0, 1, 0]

    def test_least_allocated_skips_insufficient_capacity(self):
        pool = _pool(instance_count=2, capacity=1.0,
                     allocation_policy=AllocationPolicy.LEAST_ALLOCATED)
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

    def test_cross_window_submit_rejected(self):
        pool = _pool(instance_count=2, capacity=1.0)
        with pytest.raises(ValueError, match="spans beyond"):
            pool.submit(_arrival("r1", _GIB - 32, 64))

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


class TestPoolSubmitRouting:
    def test_submit_routes_by_address(self):
        pool = _pool(instance_count=2, capacity=1.0)
        e1 = pool.submit(_arrival("r1", 0, 1000))
        e2 = pool.submit(_arrival("r2", _GIB, 1000))
        assert e1[0][0] == "r1"
        assert e2[0][0] == "r2"

    def test_submit_returns_predictions(self):
        pool = _pool(instance_count=2, capacity=1.0)
        entries = pool.submit(_arrival("r1", _GIB, 1000))
        assert len(entries) == 1
        rid, ft, m = entries[0]
        assert rid == "r1"
        assert m.size_bytes == 1000

    def test_engine_scope_instances_independent(self):
        """ENGINE scope: requests on different engines never contend."""
        pool = _pool(instance_count=2, capacity=1.0, bandwidth=100.0)
        e1 = pool.submit(_arrival("r1", 0, 1000))
        e2 = pool.submit(_arrival("r2", _GIB, 1000))
        # Different engines: each gets full bandwidth, same finish time.
        assert e1[0][1] == pytest.approx(e2[0][1])


class TestPoolFactory:
    def test_engines_created(self):
        pool = _pool(instance_count=3, capacity=1.0)
        assert len(pool.instances) == 3

    def test_capacity_is_sum(self):
        pool = _pool(instance_count=4, capacity=2.0)
        total = sum(e.capacity_bytes for e in pool.instances)
        assert total == 8 * _GIB

    def test_requires_at_least_one_engine(self):
        with pytest.raises(ValueError, match="at least one engine"):
            MemoryPool([])

    def test_pool_config_defaults(self):
        config = MemoryPoolConfig(instance_count=2)
        assert config.allocation_policy is AllocationPolicy.LEAST_ALLOCATED
