"""MemEngine — run memory simulation from a JSON config file.

Usage:
    python -m storage_mem_sim.run -c configs/analytic.json
    python -m storage_mem_sim.run -c configs/analytic.json \\
        -w configs/workloads/kv_sparse_page.json
    python -m storage_mem_sim.run -c configs/analytic_pool.json \\
        --des-schedule configs/des_demo.json
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
    p.add_argument(
        "--des-schedule",
        default=None,
        help="Discrete-event schedule JSON file (requires pool >= 1 instances "
             "and an Analytic backend)",
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
    from .memory_metrics import MemoryMetrics
    from .memory_type import MemoryRequestType, MemoryType
    from .memory_pool import MemoryPool
    from .workload.kv_cache_load import (
        KVCacheLoadGenerator,
        KVPageLayout,
        load_kv_cache_load_config,
    )

    args = parse_args(argv)
    exclusive_count = sum([
        bool(args.workload),
        bool(args.des_schedule),
    ])
    if exclusive_count > 1:
        raise ValueError(
            "-w, --des-schedule are mutually exclusive"
        )
    if args.workload and (
        args.num_requests is not None or args.size is not None
    ):
        raise ValueError(
            "--num-requests and --size cannot be combined with --workload"
        )
    if args.des_schedule and (
        args.num_requests is not None or args.size is not None or args.workload
    ):
        raise ValueError(
            "--des-schedule cannot be combined with --workload, "
            "--num-requests, or --size"
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
    request_size = args.size or mc.get("request_size", 512 * 8)

    # ---- pool settings (optional) ----
    pool_block = raw.get("mem_pool", {})
    instances = pool_block.get("instances", mc.get("instances", 1))
    if args.des_schedule and backend is not MediaSystemBackend.ANALYTIC:
        raise ValueError(
            "--des-schedule requires the Analytic backend, "
            f"got {media_type}"
        )
    if backend is not MediaSystemBackend.ANALYTIC and instances > 1:
        print("Warning: instances > 1 with non-Analytic backend; only "
              "sync issue_request (max-time semantics) is available, "
              "event mode is not supported in this phase.", flush=True)

    # ---- build MediaConfig ----
    capacity_raw = mc.get("capacity", 32.0)
    if instances > 1:
        per_gib = capacity_raw / instances
        total_gib = capacity_raw
        print("=" * 60)
        print(
            f"Pool: capacity={total_gib:.1f} GB is interpreted as TOTAL "
            f"capacity; per-instance capacity = {total_gib:.1f} / "
            f"{instances} = {per_gib:.1f} GiB"
        )
        print("=" * 60)
    else:
        per_gib = capacity_raw

    media_cfg = MediaConfig(
        media_type=backend,
        capacity=per_gib,
        bandwidth=mc.get("bandwidth", 100.0),
        config_path=os.path.abspath(mc["config"]) if mc.get("config") else "",
        # MQSim-specific
        ssd_config_path=os.path.abspath(mc["ssd_config"]) if mc.get("ssd_config") else "",
        workload_config_path=os.path.abspath(mc["workload_config"]) if mc.get("workload_config") else "",
        request_size_bytes=request_size,
        merge_contiguous=mc.get("merge_contiguous", True),
    )

    engine_cfg = MemoryEngineConfig(
        memory_type=mem_type,
        media_config=media_cfg,
        dp_size=mc.get("dp", 1) if instances == 1 else 1,
        storage_instance_num=1 if instances > 1 else mc.get("instances", 1),
    )

    # ---- build engine or pool ----
    use_pool = instances > 1
    if use_pool:
        pool = MemoryPool(engine_cfg, instances)
        engine = pool.instances[0]
        ms = engine.media_system
        tx_bytes = getattr(ms, '_tx_bytes', request_size)
    else:
        pool = None
        engine = MemoryEngine(engine_cfg)
        ms = engine.media_system
        tx_bytes = getattr(ms, '_tx_bytes', request_size)

    # MQSim trace config: override request_size per scenario
    if backend == MediaSystemBackend.MQSIM:
        ms.trace_config.request_size = request_size

    # ---- optional KV-cache workload ----
    generated_workload = None
    workload_cfg = None
    if args.workload:
        workload_cfg = load_kv_cache_load_config(args.workload)
        if workload_cfg.base_addr != 0:
            raise ValueError(
                "base_addr must be omitted or zero in a CLI workload config; "
                "run.py allocates the KV region"
            )
        region_size = KVPageLayout.required_region_size(
            context_tokens=workload_cfg.context_tokens,
            token_size_bytes=workload_cfg.token_size_bytes,
            page_size_tokens=workload_cfg.page_size_tokens,
            page_alignment_bytes=workload_cfg.page_alignment_bytes,
        )
        if use_pool and region_size > per_gib * (1024 ** 3):
            raise ValueError(
                f"KV region requires {region_size / (1024 ** 3):.2f} GiB but "
                f"each instance only has {per_gib:.1f} GiB with "
                f"{instances} instances. A KV region must fit entirely inside "
                "one instance window."
            )

        if use_pool:
            base_addr, _ = pool.get_tensor_addr(region_size)
        else:
            # Old path: instances=1 only.
            if engine.mem_config.storage_instance_num != 1:
                raise ValueError(
                    "KV-cache workload currently requires "
                    "storage_instance_num=1 or the pool path"
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
          f"Inst: {instances}")
    if generated_workload is not None:
        print("Workload:   kv_cache_load")
        print(f"Pattern:    {workload_cfg.pattern.value}")
        print(f"Granularity: {workload_cfg.granularity.value}")
        print(f"Token size: {workload_cfg.token_size_bytes} B")
        print(f"Page size:  {workload_cfg.page_size_tokens} tokens")
        print(f"Seed:       {workload_cfg.seed}")
    print("=" * 60)

    # ---- issue requests ----
    if args.des_schedule:
        # ---- discrete-event simulation mode ----
        if pool is None:
            pool = MemoryPool(engine_cfg)

        with open(args.des_schedule) as f:
            entries = json.load(f)

        from .memory_request import MemoryRequest
        from .des import SimpleSimulator

        sim = SimpleSimulator(pool)

        for entry in entries:
            t = float(entry["time"])
            source_id = entry["source_id"]
            size_bytes = int(entry["size_bytes"])
            req_type_name = entry.get("req_type", "kread").upper()
            try:
                req_type = MemoryRequestType[req_type_name]
            except KeyError:
                req_type = MemoryRequestType.KREAD
            addr, _ = pool.get_tensor_addr(size_bytes)
            sim.schedule_arrival(
                time=t,
                source_id=source_id,
                size_bytes=size_bytes,
                req_type=req_type,
                addr=addr,
            )

        result = sim.run()

        print(f"Events processed:   {len(result.request_metrics)}")
        print(f"Makespan:           {result.makespan * 1e9:.1f} ns")
        print(f"Scheduled finishes: {result.scheduled_finish_events}")
        print(f"Stale events:       {result.stale_finish_events}")
        print()

        for m in result.request_metrics:
            print(
                f"[{m.request_id}] src={m.source_id} "
                f"engine={m.mem_engine_id} "
                f"arr={m.arrival_time*1e9:.1f}ns "
                f"fin={m.finish_time*1e9:.1f}ns "
                f"lat={m.latency*1e9:.1f}ns "
                f"standalone={m.standalone_time*1e9:.1f}ns "
                f"contention={m.contention_delay*1e9:.1f}ns "
                f"bw={m.average_bandwidth/1e9:.2f}GB/s"
            )

        # ---- save output ----
        _out_dir = os.path.join(os.path.dirname(__file__), "output")
        _json_path = os.path.join(_out_dir, "des_result.json")
        _html_path = os.path.join(_out_dir, "des_result.html")
        result.save_json(_json_path)
        result.save_html(_html_path)
        print(f"\nJSON: {_json_path}")
        print(f"HTML: {_html_path}")

    elif generated_workload is not None:
        metrics = generated_workload.issue(pool if use_pool else engine)
    else:
        if use_pool:
            addrs = [
                pool.get_tensor_addr(request_size)[0]
                for _ in range(num_requests)
            ]
        else:
            addr = engine.get_tensor_addr(num_requests * request_size)
            addrs = [addr + i * request_size for i in range(num_requests)]

        if use_pool:
            # Route global addresses → per-engine batches, aggregate
            per_engine: dict = {}
            for i in range(num_requests):
                eng = pool.resolve_engine(addrs[i], request_size)
                local = addrs[i] - eng.global_base
                per_engine.setdefault(
                    eng.instance_id, (eng, [], [], [])
                )
                desc = per_engine[eng.instance_id]
                desc[1].append(local)
                desc[2].append(request_size)
                desc[3].append(MemoryRequestType.KREAD)

            total_time = 0.0
            total_cycles = 0
            total_reqs = 0
            total_bytes = 0
            iops_values = []
            for _, (eng, locals_, sizes, types) in per_engine.items():
                m = eng.issue_request(locals_, sizes, types)
                total_time = max(total_time, m.total_time)
                total_cycles = max(total_cycles, m.cycles)
                total_reqs += m.memory_reqs_num
                total_bytes += sum(sizes)
                if m.iops is not None:
                    iops_values.append(m.iops)

            metrics = MemoryMetrics(
                cycles=total_cycles,
                total_time=total_time,
                memory_reqs_num=total_reqs,
                global_memory_reqs_num=total_reqs,
                bandwidth=(total_bytes / total_time if total_time > 0 else 0.0),
                iops=sum(iops_values) if iops_values else None,
            )
        else:
            metrics = engine.issue_request(
                addrs,
                [request_size] * num_requests,
                [MemoryRequestType.KREAD] * num_requests,
            )

    # ---- results ----
    if not args.des_schedule:
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
        bw_target = (
            engine if not use_pool else pool.get_engine(0)
        )
        print(f"Bandwidth: "
              f"{bw_target.get_engine_metrics().bandwidth / 1e9:.2f} GB/s")

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
