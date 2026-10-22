"""Analytic backend integration for the KV-cache load workload."""

import pytest

from ....media import MediaConfig, MediaSystemBackend
from ....memory_config import MemoryEngineConfig
from ....memory_engine import MemoryEngine
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
    assert metrics.global_memory_reqs_num == generated.stats.logical_requests


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

