"""Ramulator integration tests through the public MemoryEngine API."""

import pytest

from ....media import MediaConfig, MediaSystemBackend
from ....memory_engine import MemoryEngineConfig
from ....memory_engine import MemoryEngine
from ....memory_type import MemoryType
from ....workload.kv_cache_load import (
    KVAccessPattern,
    KVCacheLoadConfig,
    KVCacheLoadGenerator,
    KVLoadGranularity,
)


@pytest.mark.ramulator_native
@pytest.mark.parametrize(
    ("pattern", "granularity", "token_size"),
    [
        (KVAccessPattern.CONTIGUOUS, KVLoadGranularity.TOKEN, 576),
        (KVAccessPattern.SPARSE_UNIFORM, KVLoadGranularity.TOKEN, 640),
        (KVAccessPattern.CONTIGUOUS, KVLoadGranularity.PAGE, 576),
        (KVAccessPattern.SPARSE_PAGE_LOCAL, KVLoadGranularity.PAGE, 640),
    ],
)
def test_kv_workload_runs_through_native_ramulator(
    pattern,
    granularity,
    token_size,
):
    pytest.importorskip(
        "ramulator",
        reason="Ramulator2 Python/native binding is not installed",
    )
    engine = MemoryEngine(
        MemoryEngineConfig(
            memory_type=MemoryType.HBM,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.RAMULATOR,
                capacity=1.0,
            ),
        )
    )
    generated = KVCacheLoadGenerator().generate(
        KVCacheLoadConfig(
            access_tokens=4,
            context_tokens=64,
            token_size_bytes=token_size,
            pattern=pattern,
            granularity=granularity,
            page_size_tokens=4,
            selected_tokens_per_page=2,
            seed=5,
        )
    )

    metrics = generated.issue(engine)

    assert metrics.memory_reqs_num == generated.stats.logical_requests
    assert metrics.cycles > 0
    assert metrics.total_time > 0
    assert engine.get_engine_metrics().total_bytes == generated.stats.issued_bytes
