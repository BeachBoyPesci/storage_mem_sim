"""Analytic backend integration for the KV-cache load workload."""

import pytest

from ....media import MediaConfig, MediaSystemBackend
from ....memory_engine import MemoryEngineConfig
from ....memory_engine import MemoryEngine
from ....memory_pool import MemoryPool, MemoryPoolConfig
from ....memory_type import MemoryType
from ....workload.kv_cache_load import (
    KVAccessPattern,
    KVCacheLoadConfig,
    KVCacheLoadGenerator,
    KVLoadGranularity,
)


def _engine(bandwidth_gib_per_sec=2.0):
    return MemoryEngine(
        MemoryEngineConfig(
            memory_type=MemoryType.HBM,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                capacity=1.0,
                bandwidth=bandwidth_gib_per_sec,
            ),
        )
    )


@pytest.mark.parametrize("token_size", [576, 640])
@pytest.mark.parametrize(
    ("pattern", "granularity"),
    [
        (KVAccessPattern.CONTIGUOUS, KVLoadGranularity.TOKEN),
        (KVAccessPattern.CONTIGUOUS, KVLoadGranularity.PAGE),
        (KVAccessPattern.SPARSE_UNIFORM, KVLoadGranularity.TOKEN),
        (KVAccessPattern.SPARSE_PAGE_LOCAL, KVLoadGranularity.PAGE),
    ],
)
def test_kv_workload_runs_through_analytic(
    token_size,
    pattern,
    granularity,
):
    config = KVCacheLoadConfig(
        access_tokens=8,
        context_tokens=128,
        token_size_bytes=token_size,
        pattern=pattern,
        granularity=granularity,
        page_size_tokens=4,
        selected_tokens_per_page=2,
        seed=11,
    )
    generated = KVCacheLoadGenerator().generate(config)
    engine = _engine()
    metrics = generated.issue(engine)

    expected_time = generated.stats.issued_bytes / (2.0 * 1024**3)
    assert metrics.total_time == pytest.approx(expected_time)
    assert metrics.memory_reqs_num == generated.stats.logical_requests


def test_analytic_is_address_insensitive_for_equal_token_traffic():
    common = {
        "access_tokens": 16,
        "context_tokens": 256,
        "token_size_bytes": 576,
        "granularity": KVLoadGranularity.TOKEN,
        "page_size_tokens": 16,
    }
    contiguous = KVCacheLoadGenerator().generate(
        KVCacheLoadConfig(
            **common,
            pattern=KVAccessPattern.CONTIGUOUS,
        )
    )
    sparse = KVCacheLoadGenerator().generate(
        KVCacheLoadConfig(
            **common,
            pattern=KVAccessPattern.SPARSE_UNIFORM,
            seed=21,
        )
    )

    contiguous_metrics = contiguous.issue(_engine())
    sparse_metrics = sparse.issue(_engine())

    assert contiguous.addresses != sparse.addresses
    assert contiguous.stats.issued_bytes == sparse.stats.issued_bytes
    assert contiguous_metrics.total_time == sparse_metrics.total_time


_PAGE_BYTES = 16 * 576  # page_size_tokens * token_size_bytes
_GIB = 1024 ** 3


def _pool(instance_count=2, per_instance_gib=1.0, bandwidth_gib_per_sec=2.0):
    # media_config.bandwidth 为总带宽口径：乘回实例数保持每实例
    # bandwidth_gib_per_sec 的测试语义。
    return MemoryPool(
        MemoryPoolConfig(
            instance_count=instance_count,
            memory_type=MemoryType.HBM,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.ANALYTIC,
                capacity=per_instance_gib * instance_count,
                bandwidth=bandwidth_gib_per_sec * instance_count,
            ),
        )
    )


def _contiguous_page_workload(base_addr=0):
    """16 个 9216 B 的整页请求，从 *base_addr* 开始连续分布。"""
    return KVCacheLoadGenerator().generate(
        KVCacheLoadConfig(
            access_tokens=256,
            context_tokens=256,
            token_size_bytes=576,
            pattern=KVAccessPattern.CONTIGUOUS,
            granularity=KVLoadGranularity.PAGE,
            page_size_tokens=16,
            base_addr=base_addr,
        )
    )


def _per_engine_reqs(pool, instance_count=2):
    return [
        pool.get_engine(i).get_engine_metrics().memory_reqs_num
        for i in range(instance_count)
    ]


def test_kv_region_spans_pool_windows_page_aligned():
    """KV region 跨两个窗口且 page 与边界对齐时，请求均分到两个实例。"""
    generated = _contiguous_page_workload(base_addr=_GIB - 8 * _PAGE_BYTES)
    assert generated.stats.logical_requests == 16

    pool = _pool()
    metrics = generated.issue(pool)

    assert metrics.memory_reqs_num == 16
    assert _per_engine_reqs(pool) == [8, 8]
    expected_time = (8 * _PAGE_BYTES) / (2.0 * 1024**3)
    assert metrics.total_time == pytest.approx(expected_time)


def test_kv_page_request_split_at_window_boundary():
    """page 请求横跨窗口边界时被切成两个片段，每片段各计一次。"""
    generated = _contiguous_page_workload(
        base_addr=_GIB - 8 * _PAGE_BYTES - _PAGE_BYTES // 2
    )

    pool = _pool()
    metrics = generated.issue(pool)

    # 第 9 个 page 横跨窗口边界 → 多一个片段
    assert metrics.memory_reqs_num == 17
    assert _per_engine_reqs(pool) == [9, 8]

