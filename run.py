"""MemEngine — run memory simulation from a JSON config file.

Usage:
    python -m storage_mem_sim.run -c configs/analytic.json
    python -m storage_mem_sim.run -c configs/analytic.json \
        -w configs/workloads/kv_sparse_page.json
    python run.py -c configs/ramulator.json
"""

import argparse
from dataclasses import replace
import json
import os
import sys


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="MemEngine memory simulator")
    p.add_argument("-c", "--config", required=True, help="JSON config file")
    p.add_argument(
        "-w",
        "--workload",
        default=None,
        help="KV-cache workload JSON file",
    )
    p.add_argument("--num-requests", type=int, default=None,
                   help="Number of requests (overrides config)")
    p.add_argument("--size", type=int, default=None,
                   help="Request size in bytes (overrides config)")
    return p.parse_args(argv)


def main(argv=None):
    from .media import MediaConfig, MediaSystemBackend
    from .memory_engine import MemoryEngine
    from .memory_config import MemoryEngineConfig
    from .memory_type import MemoryRequestType, MemoryType
    from .workload.kv_cache_load import (
        KVCacheLoadGenerator,
        KVPageLayout,
        load_kv_cache_load_config,
    )

    args = parse_args(argv)

    if args.workload and (
        args.num_requests is not None or args.size is not None
    ):
        raise ValueError(
            "--num-requests and --size cannot be combined with --workload"
        )

    with open(args.config) as f:
        raw = json.load(f)

    mc = raw["media_config"]
    mem_type = MemoryType[raw["mem_type"].upper()]

    # ---- backend ----
    media_type = mc["media_type"]
    if media_type == "ramulator":
        backend = MediaSystemBackend.RAMULATOR
    elif media_type == "mqsim":
        backend = MediaSystemBackend.MQSIM
    else:
        backend = MediaSystemBackend.ANALYTIC

    # ---- common params (with CLI overrides) ----
    num_requests = args.num_requests or mc.get("num_requests", 64)
    request_size = args.size or mc.get("request_size", 512*8)

    # ---- build MediaConfig ----
    media_cfg = MediaConfig(
        media_type=backend,
        capacity=mc.get("capacity", 32.0),
        bandwidth=mc.get("bandwidth", 100.0),
        config_path=os.path.abspath(mc["config"]) if mc.get("config") else "",
        # MQSim-specific
        ssd_config_path=os.path.abspath(mc["ssd_config"]) if mc.get("ssd_config") else "",
        workload_config_path=os.path.abspath(mc["workload_config"]) if mc.get("workload_config") else "",
        request_size_bytes=request_size,
        merge_contiguous=mc.get("merge_contiguous", True),
    )

    engine = MemoryEngine(MemoryEngineConfig(
        memory_type=mem_type,
        media_config=media_cfg,
        dp_size=mc.get("dp", 1),
        storage_instance_num=mc.get("instances", 1),
    ))

    ms = engine.media_system
    tx_bytes = getattr(ms, '_tx_bytes', request_size)

    # ---- MQSim trace config: override request_size per scenario ----
    if backend == MediaSystemBackend.MQSIM:
        ms.trace_config.request_size = request_size

    # ---- optional KV-cache workload ----
    generated_workload = None
    workload_cfg = None
    if args.workload:
        if engine.mem_config.storage_instance_num != 1:
            raise ValueError(
                "KV-cache workload currently requires "
                "storage_instance_num=1"
            )
        workload_cfg = load_kv_cache_load_config(args.workload)
        if workload_cfg.base_addr != 0:
            raise ValueError(
                "base_addr must be omitted or zero in a CLI workload config; "
                "run.py allocates the KV region through MemoryEngine"
            )
        region_size = KVPageLayout.required_region_size(
            context_tokens=workload_cfg.context_tokens,
            token_size_bytes=workload_cfg.token_size_bytes,
            page_size_tokens=workload_cfg.page_size_tokens,
            page_alignment_bytes=workload_cfg.page_alignment_bytes,
        )
        base_addr = engine.get_tensor_addr(region_size)
        workload_cfg = replace(workload_cfg, base_addr=base_addr)
        generated_workload = KVCacheLoadGenerator().generate(workload_cfg)

    # ---- status banner ----
    print("=" * 60)
    print(f"Mem type:   {mem_type.value}")
    print(f"Backend:    {media_type}")
    if media_type == "ramulator":
        io_freq = getattr(ms, '_io_frequency_mhz', None)
        print(f"Tick freq:  {io_freq} MHz  |  tx_bytes: {tx_bytes}")
        if mc.get("config"):
            print(f"YAML:       {mc['config']}")
    elif media_type == "mqsim":
        print(f"Merge:      {mc.get('merge_contiguous', True)}")
        print(f"Trace slice:{media_cfg.request_size_bytes:>8} B")
        if generated_workload is None:
            print(f"Req size:   {request_size} B")
            print(f"Num reqs:   {num_requests}")
        if mc.get("ssd_config"):
            print(f"SSD config: {mc['ssd_config']}")
        if mc.get("workload_config"):
            print(f"Workload:   {mc['workload_config']}")
        if hasattr(ms, 'mqsim_available'):
            status = "loaded" if ms.mqsim_available else "NOT BUILT"
            print(f"_mqsim:     {status}")
            if not ms.mqsim_available:
                print(f"  Build: cd media/mqsim_wrapper && pip install -e .")
    else:
        print(f"Bandwidth:  {mc.get('bandwidth', 100.0)} GB/s")
    print(f"Capacity:   {mc.get('capacity', 32.0)} GB  |  "
          f"DP: {mc.get('dp', 1)}  |  Inst: {mc.get('instances', 1)}")
    if generated_workload is not None:
        print("Workload:   kv_cache_load")
        print(f"Pattern:    {workload_cfg.pattern.value}")
        print(f"Granularity: {workload_cfg.granularity.value}")
        print(f"Token size: {workload_cfg.token_size_bytes} B")
        print(f"Page size:  {workload_cfg.page_size_tokens} tokens")
        print(f"Seed:       {workload_cfg.seed}")
    print("=" * 60)

    # ---- issue requests ----
    if generated_workload is not None:
        metrics = generated_workload.issue(engine)
    else:
        addr = engine.get_tensor_addr(num_requests * request_size)
        addrs = [addr + i * request_size for i in range(num_requests)]

        metrics = engine.issue_request(
            addrs,
            [request_size] * num_requests,
            [MemoryRequestType.KREAD] * num_requests,
        )

    # ---- results ----
    if generated_workload is not None:
        stats = generated_workload.stats
        print(f"Tokens:    {stats.selected_tokens}")
        print(f"Pages:     {stats.unique_pages}")
        print(f"Requests:  {stats.logical_requests} "
              f"(global: {metrics.global_memory_reqs_num})")
        print(f"Demand:    {stats.demand_bytes} B")
        print(f"Issued:    {stats.issued_bytes} B")
    else:
        print(f"Requests:  {num_requests} × {request_size}B → "
              f"{metrics.memory_reqs_num} memory reqs "
              f"(global: {metrics.global_memory_reqs_num})")
    if backend == MediaSystemBackend.RAMULATOR:
        print(f"Cycles:    {metrics.cycles}")
    print(f"Time:      {metrics.total_time * 1e9:.1f} ns")
    print(f"Bandwidth: {engine.get_engine_metrics().bandwidth / 1e9:.2f} GB/s")

    print("=" * 60)


if __name__ == "__main__":
    # Bootstrap: when run as 'python run.py', set up sys.path so the
    # project is importable as a package, then run main() in that context.
    _proj_root = os.path.dirname(os.path.abspath(__file__))
    _parent = os.path.dirname(_proj_root)
    if _parent not in sys.path:
        sys.path.insert(0, _parent)
    import importlib
    importlib.import_module(
        f"{os.path.basename(_proj_root)}.run").main()
