# MemEngine — Memory/Storage Media Simulator Design Document

## 1. Overview

**MemEngine** is a multi-backend memory/storage media simulator framework designed for LLM inference serving scenarios. It sits between the **service/model layer** (LLM inference pipeline: Prefill/Decode, KV Cache read/write) and the **media simulation layer** (Ramulator, MQSim, Analytic models), providing:

- **Address space management** — monotonic global address allocation with alignment
- **Request construction** — decomposition of high-level memory objects into media-level requests
- **Multi-instance routing** — provided by `MemoryPool` on top of single-instance `MemoryEngine`
- **Multi-backend simulation** — pluggable backends for different fidelity/performance trade-offs

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                  Service / Model Layer                       │
│  (LLM Inference: Prefill / Decode, KV Cache R/W)            │
└──────────────────────┬──────────────────────────────────────┘
                       │ GetTensorAddr / issue_request
                       ▼
┌─────────────────────────────────────────────────────────────┐
│               MemoryEngine (memengine/)                      │
│  ┌───────────┐  ┌──────────────┐                            │
│  │ Address    │  │ Request      │                            │
│  │ Allocation │  │ Construction │                            │
│  └───────────┘  └──────────────┘                            │
│                                                             │
│  MemoryRequest → MediaRequest                               │
│  └─────────────────────────────────────────────┘            │
│                      │ handler_mem_request                   │
└──────────────────────┼──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│               MediaSystem (media/)                           │
│  ┌────────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │ Analytic   │  │ Ramulator2   │  │ MQSim (SSD)          │ │
│  │ Roofline   │  │ Cycle-Accurate│ │ Event-Driven         │ │
│  └────────────┘  └──────────────┘  └──────────────────────┘ │
│  MediaMetrics ← per-backend results (cycles / time)         │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Backend Comparison

| Backend | Type | Fidelity | Speed | Use Case |
|---------|------|----------|-------|----------|
| **Analytic** | Pure analytical model | Low (size/bandwidth only) | Instant | Fast prototyping, roofline estimation |
| **Ramulator2.1** | Cycle-accurate DRAM simulator | High (DRAM timing, controllers) | Medium | Detailed DRAM performance analysis |
| **MQSim** | Event-driven SSD simulator | High (Flash, FTL, NVMe) | Slow | SSD/NVMe device simulation |

---

## 3. Package Structure

```
storage_mem_sim/
├── memory_engine.py              # Engine config + main engine class
├── memory_request.py             # Engine-level request
├── memory_type.py                # Enums (MemoryType, RequestType)
├── memory_metrics.py             # Metrics containers
├── memory_pool/                  # Multi-instance pool
│   ├── __init__.py
│   └── memory_pool.py            # MemoryPoolConfig + MemoryPool
├── run.py                        # JSON-driven CLI
├── workload/                     # High-level workloads
│   └── kv_cache_load/            # KV cache load generator
├── media/                        # Media System layer
│   ├── __init__.py
│   ├── base_media.py             # Abstract base class
│   ├── media_config.py           # Media configuration
│   ├── media_request.py          # Media-level request
│   ├── media_metrics.py          # Media metrics containers
│   ├── media_backend.py          # Backend enum
│   ├── media_system_factory.py   # Factory pattern
│   ├── analytic_media_system.py  # Analytic backend
│   ├── ramulator_media_system.py # Ramulator2 backend
│   ├── mqsim_media_system.py     # MQSim backend
│   ├── ramulator_wrapper/        # Ramulator2 integration (submodule + pybind11)
│   └── mqsim_wrapper/            # MQSim integration (submodule + pybind11 + pymqsim)
├── configs/                      # Example configs (JSON / YAML / XML)
├── docs/                         # Documentation
└── tests/                        # Test cases
    ├── test_memory_engine.py
    ├── test_memory_request.py
    ├── test_memory_pool.py
    ├── test_analytic_media.py
    ├── test_ramulator_media.py
    └── test_mqsim_media.py
```

---

## 4. MemEngine Core Design

### 4.1 Address Space Management

`MemoryEngine` maintains a monotonically increasing local allocation water level `global_addr`; addresses are local to the instance. A global address space exists only at the `MemoryPool` level (global = instance `global_base` + local). Each tensor allocation returns an aligned base address:

```python
def get_tensor_addr(self, size: int) -> int:
    # Align to granularity (default 64B / cache line)
    # Check against mem_config.capacity
    # Return start_addr
```

- **Alignment granularity**: configurable, default 64 bytes (cache line)
- **Capacity check**: raises `OverflowError` if `global_addr > mem_config.capacity`

### 4.2 Request Construction Flow

```
issue_request(addr, size, req_type)
  → MemoryRequest(addr, size, req_type, config)
  → media_system.handler_mem_request(mem_reqs)
  → MemoryMetrics returned
```

### 4.3 Data Structures

| Structure | File | Purpose |
|-----------|------|---------|
| `MemoryRequest` | `memory_request.py` | One logical access: addr, size, req_type; estimates `media_req_num = ceil(size/granularity)`; maintains `media_request_list: List[MediaRequest]` |
| `MediaRequest` | `media/media_request.py` | Media-level request with addr, addr_vec, req_type (0=Read, 1=Write) |

### 4.4 Single-Instance Semantics / Multi-Instance Routing

`MemoryEngine` models exactly one physical media instance: requests are passed
to the backend as-is, with no DP replication and no instance fan-out.

Multi-instance semantics are expressed by `memory_pool.MemoryPool`:

1. The pool owns a global address space partitioned into per-instance windows.
2. Each request is split at window boundaries; every fragment is routed to
   the owning instance's `MemoryEngine` (global→local address translation).
3. Instances execute in parallel; pool-level `time = max(per-instance time)`
   and `bandwidth = total_bytes / max_time`.

### 4.5 Configuration

```python
class MemoryEngineConfig:
    memory_type: MemoryType          # HBM / DDR / SSD
    media_config: MediaConfig        # Media layer config
    granularity: int                 # init=False; derived from backend at engine init
                                     # (Analytic/MQSim: 64 B, Ramulator: _tx_bytes)
    capacity: int                    # Instance capacity (bytes), derived from media_config.capacity
```

### 4.6 Metrics

```
MemoryMetrics (single issue_request result)
  ├── cycles: int              ← from MediaMetrics
  ├── total_time: float        ← from MediaMetrics
  ├── memory_reqs_num: int     ← engine-level (fragment) request count
  ├── bandwidth: float         ← B/s reported by backend
  ├── iops / iops_read / iops_write
  │                              ← MQSim end-to-end device rates;
  │                                None for Analytic/Ramulator;
  │                                pool: mean across instances

MemoryEngineMetrics (cumulative)
  ├── cycles / total_time / memory_reqs_num (accumulated)
  ├── mem_metrics_list: List[MemoryMetrics]
  ├── bandwidth: float         ← total simulated bytes / total time
  └── iops*                    ← time-weighted MQSim device rates;
                                 None for Analytic/Ramulator
```

---

## 5. MediaSystem Design

### 5.1 Abstract Base Class

```python
class BaseMediaSystem(ABC):
    def __init__(self, config: MediaConfig): ...
    
    @abstractmethod
    def handler_mem_request(self, mem_req: List[MemoryRequest]) -> MediaMetrics: ...
```

### 5.2 MediaConfig

```python
class MediaConfig:
    media_type: MediaSystemBackend   # RAMULATOR / MQSIM / ANALYTIC
    config_path: str                 # Backend config file path (YAML or XML)
    granularity: int = 64            # Access granularity (bytes)
    capacity: float                  # Capacity in GiB (total across instances for MemoryPoolConfig)
    bandwidth: float                 # Bandwidth in GiB/s (Analytic backend)
    scale_factor: float              # Cycles → time conversion factor
```

### 5.3 Factory Pattern

```python
class MediaSystemFactory:
    _backends = {
        MediaSystemBackend.RAMULATOR: RamulatorMediaSystem,
        MediaSystemBackend.MQSIM: MQSimMediaSystem,
        MediaSystemBackend.ANALYTIC: AnalyticMediaSystem,
    }
    
    @classmethod
    def create(cls, config: MediaConfig) -> BaseMediaSystem: ...
```

### 5.4 Metrics

```python
class MediaMetrics:
    num_read_requests: int
    num_write_requests: int
    num_other_requests: int
    cycles: int               # Ramulator backend fills this
    num_media_reqs: int
    time: float               # Simulation time (seconds), Analytic/MQSim fill this

class MediaSystemMetrics:     # Cumulative version
    # Includes update_from_media() and media_metrics_list
```

---

## 6. Backend Implementations

### 6.1 Analytic Backend (Roofline Estimation)

Pure analytical model: `total_time = Σ (size / bandwidth)`. No cycle-level simulation. Suitable for rapid prototyping and roofline analysis.

```python
class AnalyticMediaSystem(BaseMediaSystem):
    def handler_mem_request(self, mem_req_list):
        total_time = (
            sum(req.size for req in mem_req_list)
            / self._bandwidth_bytes_per_sec  # bandwidth (GiB/s) * 1024**3
        )
        return MediaMetrics(time=total_time)
```

### 6.2 Ramulator2 Backend (DRAM Cycle-Accurate Simulation)

Integrates Ramulator2 v2.1 via C++ pybind11 extension (`ramulator_backend` module).

**Workflow:**
1. Convert `MemoryRequest` → `List[MediaRequest]` (split by granularity)
2. Convert `MediaRequest` → C++ `Request` via pybind11
3. `send_requests()` → `run()` → `get_metrics()`
4. Return `MediaMetrics(cycles, time=cycles * scale_factor)`

**Build:** `cd media/ramulator_wrapper && pip install -e .`

### 6.3 MQSim Backend (SSD Event-Driven Simulation)

Integrates MQSim via C++ pybind11 extension (`mqsim_backend` module).

**Workflow:**
1. Write `MemoryRequest` list to trace text file (format: `0 0 <start_lba> <sectors> <req_type>`)
2. Patch workload XML with trace file path
3. Call `MQSimAdapter.run(ssd_config, workload_config)`
4. Estimate time: `total_time = total_bytes / bandwidth_bps`
5. Return `MediaMetrics(time)` (no cycles)

**Build:** `cd media/mqsim_wrapper && pip install -e .`

**Key Config:**
| Key | Meaning |
|-----|---------|
| `ssd_config` | SSD parameter XML path |
| `workload_config` | Workload definition XML path |
| `trace_output_path` | Generated trace file path |
| `sector_bytes` | Sector size (default 512B) |
| `max_sectors_per_nvme_io` | Max sectors per NVMe request |

---

## 7. Data Flow Summary

```
  Model/Layer
      │  GetTensorAddr(64MB) → start_addr
      │  issue_request([addr], [size], [KREAD])
      ▼
  MemoryEngine
      │  align_up(size) → granularity (64B) alignment
      │  MemoryRequest(addr, size, type, config) → media_req_num = size/64
      ▼
  MediaSystem.handler_mem_request(List[MemoryRequest])
      │
      ├─ Analytic:    total_time = Σ(size / bandwidth)
      │
      ├─ Ramulator2:  MemoryRequest → List[MediaRequest(addr, type)]
      │               → ramulator_backend.convert_request_list()
      │               → send_requests() → run() → get_metrics()
      │               → MediaMetrics(cycles, time)
      │
      └─ MQSim:       MemoryRequest → trace file
                      → patch workload XML
                      → MQSimAdapter.run(ssd_cfg, workload_cfg)
                      → total_time = total_bytes / bandwidth_bps
                      → MediaMetrics(time)
      │
      ▼
  MemoryMetrics ← update_from_media(media_metrics)
      │
      ▼
  Return to caller (LLM inference pipeline)
```

---

## 8. Extension Guide: Adding a New Media Backend

1. Add new type to `MediaSystemBackend` enum in `media/media_backend.py`
2. Create a new `*_media_system.py` file in `media/`, inheriting `BaseMediaSystem` and implementing `handler_mem_request`
3. Register the new class in `MediaSystemFactory._backends` dict in `media/media_system_factory.py`
4. Optionally: implement a C++ pybind11 extension if the backend is a native simulator
5. Add corresponding tests in `tests/`

---

## 9. Environment & Dependencies

- **Python**: 3.10+
- **Ramulator2**: C++17, pybind11 (submodule: `media/ramulator_wrapper/ramulator2`, branch `v2.1`)
- **MQSim**: C++17, pybind11 (submodule: `media/mqsim_wrapper/MQSim`)
- **Build tools**: CMake, setuptools, pybind11

## 10. References

- [Ramulator2 v2.1](https://github.com/Yogaht/ramulator2/tree/v2.1)
- [MQSim](https://github.com/Yogaht/MQSim)
- [memengine.md](../memengine.md) — Original architecture specification
