"""MQSim integration tests through the public MemoryEngine API."""

import pytest

from ....media import MediaConfig, MediaSystemBackend
from ....media import mqsim_media_system
from ....media.mqsim_wrapper.pymqsim import check_mqsim_available
from ....memory_engine import MemoryEngineConfig
from ....memory_engine import MemoryEngine
from ....memory_type import MemoryType
from ....workload.kv_cache_load import (
    KVAccessPattern,
    KVCacheLoadConfig,
    KVCacheLoadGenerator,
    KVLoadGranularity,
)


@pytest.mark.mqsim_native
@pytest.mark.parametrize(
    ("pattern", "granularity", "token_size"),
    [
        (KVAccessPattern.CONTIGUOUS, KVLoadGranularity.TOKEN, 576),
        (KVAccessPattern.SPARSE_UNIFORM, KVLoadGranularity.TOKEN, 640),
        (KVAccessPattern.CONTIGUOUS, KVLoadGranularity.PAGE, 576),
        (KVAccessPattern.SPARSE_PAGE_LOCAL, KVLoadGranularity.PAGE, 640),
    ],
)
def test_kv_workload_runs_through_native_mqsim(
    pattern,
    granularity,
    token_size,
    monkeypatch,
    tmp_path,
):
    if not check_mqsim_available():
        pytest.skip("MQSim pybind11 binding is not built")

    # Keep backend-generated trace/XML artifacts inside this test's temporary
    # directory without changing production backend behavior.
    monkeypatch.setattr(
        mqsim_media_system,
        "__file__",
        str(tmp_path / "mqsim_media_system.py"),
    )
    engine = MemoryEngine(
        MemoryEngineConfig(
            memory_type=MemoryType.SSD,
            media_config=MediaConfig(
                media_type=MediaSystemBackend.MQSIM,
                capacity=1.0,
                request_size_bytes=131072,
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
            seed=3,
        )
    )

    metrics = generated.issue(engine)

    assert metrics.memory_reqs_num == generated.stats.logical_requests
    assert metrics.total_time > 0
    assert metrics.bandwidth > 0
    assert metrics.iops > 0
    assert engine.get_engine_metrics().total_bytes == generated.stats.issued_bytes
