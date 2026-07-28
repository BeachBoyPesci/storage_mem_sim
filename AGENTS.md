# MemEngine 项目指南

本文件是本仓库面向 AI 编程代理和开发者的项目级知识入口。开始工作前先读本文件；涉及具体后端时，再读“相关文档”中对应的专题文档。代码与测试是最终事实来源，文档和实现冲突时以当前实现为准，并在同一变更中修正文档。

## 项目目标与设计哲学

MemEngine 是面向 LLM 推理服务的内存/存储介质仿真框架。它把 tensor、权重和 KV cache 等高层访问抽象成统一请求，再交给不同精度和成本的介质后端执行，输出可比较的时间、周期、带宽和 IOPS 指标。

核心设计原则：

1. **上层语义与后端细节解耦**：调用者只使用 `MemoryEngine`、字节地址、字节大小和读写类型；DRAM transaction、SSD trace、XML/YAML 等细节留在 `media/`。
2. **一套请求模型，多种精度**：优先用 Analytic 建立 roofline/数量级基线，再用 Ramulator2 或 MQSim 解释微架构效应。不同后端回答的问题不同，不能把低精度结果解释成周期级结论。
3. **配置驱动而非硬编码**：容量、带宽、DP、实例数和器件组织来自配置。Ramulator transaction 大小和时钟由 DRAM spec 推导；MQSim NAND 几何从 SSD XML 加载。
4. **单位必须显式**：公共地址和大小为 byte，容量配置为 GiB（实现使用 `1024**3`），Analytic 带宽配置为 GiB/s，内部带宽指标为 B/s，时间为 s，Ramulator 频率为 MHz，MQSim 延迟通常为 ns。
5. **仿真假设必须可追溯**：性能结论必须同时记录 workload、后端、硬件配置、请求变换、合并/切片策略和指标口径，不能只报告一个最终数字。
6. **后端适配层保持薄而可测**：公共生命周期在 engine；格式转换、运行和输出解析在后端 wrapper。扩展新后端时实现接口并注册工厂，不把后端分支散落进 engine。

## 仓库边界

项目自研代码主要是：

- 根目录 Python 文件：engine、请求、配置、指标和 CLI。
- `workload/`：位于 engine 之上的高层负载生成器；当前包含 KV cache load。
- `media/*.py`：公共介质层与三个后端适配器。
- `media/mqsim_wrapper/pymqsim/`、`mqsim_pybind.cpp` 和相关 CMake/setup：MQSim 自研桥接层。
- `configs/`、`docs/`、`tests/`。

以下目录是外部代码或生成物，除非任务明确要求，不要修改，也不要把它们当成本项目编码风格样例：

- `media/ramulator_wrapper/ramulator2/`：Git submodule，Ramulator2 上游/定制仓库。
- `media/mqsim_wrapper/MQSim/`：Git submodule，MQSim 上游/定制仓库。
- `media/mqsim_wrapper/ext/pybind11/`：第三方依赖。
- `build/`、`*.egg-info/`、`.pytest_cache/`、`.cache/`、动态库和运行生成的 trace/XML：构建或运行产物。

工作区可能已有用户未提交变更。修改前运行 `git status --short`，不要覆盖、清理或顺手格式化无关文件；尤其不要重置 submodule 状态。

## 架构与数据流

```text
service/model
        |
        v
KV-cache workload (optional)
  token selector -> page layout -> request generator
        |
        | get_tensor_addr / issue_request
        v
MemoryEngine
  address allocation -> DP replication -> instance distribution
        |
        v
MemoryObject -> MemoryRequest
        |
        v
MediaSystemFactory -> BaseMediaSystem
        |                 |                 |
     Analytic         Ramulator2          MQSim
  bytes/bandwidth   tx decomposition   merge/slice -> trace
                    -> DRAM cycles     -> workload XML -> SSD sim
        |                 |                 |
        +---------- MediaMetrics -----------+
                          |
                          v
             MemoryMetrics / cumulative metrics
```

一次 `issue_request(addr, size, req_type)` 的准确流程：

1. 校验三个列表等长、地址非负、大小严格大于 0。
2. 每个输入请求复制到所有 DP rank；rank 地址偏移为 `dp_rank * per_dp_capacity`。
3. 复制后的请求按顺序 round-robin 分配到 storage instances。
4. 多实例被建模为同构并行实例，当前实现只模拟第一个非空实例；其耗时代表实例关键路径，而 `global_memory_reqs_num` 保留全局请求数。不要误写成“模拟并累加所有实例”。
5. 后端返回 `MediaMetrics`，engine 映射为单次 `MemoryMetrics` 并累积到 `MemoryEngineMetrics`。

地址分配也是模型状态：`get_tensor_addr()` 按 backend granularity 向上对齐，单调推进 `global_addr`，并以 `per_dp_capacity` 检查溢出；只有显式调用 `reset_addr()` 才归零。Engine 初始化后 granularity 对 Analytic/MQSim 通常为 64 B，对 Ramulator 为器件推导出的 `_tx_bytes`。

## 模块职责

| 模块 | 责任与关键约束 |
| --- | --- |
| `memory_engine.py` | 唯一的高层入口；地址分配、参数校验、DP 复制、实例分发、后端调用与累计指标。不要在此解析 YAML/XML 或实现器件时序。 |
| `memory_config.py` | Engine 配置及容量派生：`total_capacity`、`per_dp_capacity`、每实例 `capacity`。`media_config` 应在构造时提供，避免构造后修改导致派生值过期。 |
| `memory_type.py` | `MemoryType` 和 `MemoryRequestType`；介质读写编码固定为 read=0、write=1。注意 MQSim trace 自身使用 read=1、write=0，由 trace 层转换。 |
| `memory_object.py` | 一个逻辑访问；记录 addr/size/type，并按 engine granularity 估算 `media_req_num`。实际介质请求数以 backend 返回值为准。 |
| `memory_request.py` | 逻辑访问的轻量容器；可保存拆分后的 `MediaRequest`。 |
| `memory_metrics.py` | Engine 单次与累计指标。累计带宽为”模拟实例传输字节数 / 累计时间”；IOPS 只透传并按时间聚合 MQSim 的端到端 device IOPS，Analytic/Ramulator 为 `None`。 |
| `media/base_media.py` | 后端抽象接口 `handler_mem_request(List[MemoryRequest]) -> MediaMetrics` 与后端累计指标。 |
| `media/media_config.py` | 公共及 backend-specific 配置；保持公共字段的单位兼容性。 |
| `media/media_system_factory.py` | 后端注册和惰性创建。新增后端应扩展 enum、实现类、注册逻辑和测试。 |
| `media/analytic_media_system.py` | `sum(bytes) / configured bandwidth`；无排队、并发、固定延迟或地址效应，是吞吐上界基线。 |
| `media/ramulator_media_system.py` | 读取 Ramulator YAML，推导 transaction bytes/频率，按覆盖的 transaction 边界拆请求，生成临时 LD/ST trace，组装并运行 Ramulator2。多 controller 周期取最大值。 |
| `media/mqsim_media_system.py` | 协调 SSD XML 几何加载、trace 生成、workload XML 生成、native MQSim 执行与结果映射。 |
| `media/mqsim_wrapper/pymqsim/trace.py` | byte address 到 LBA、相邻同类型请求合并、request-size 切片、CWDP 地址布局及理论上界公式。几何函数调用前必须加载 SSD XML。 |
| `media/mqsim_wrapper/pymqsim/workload.py` | 用模板生成 workload XML，只负责替换 trace 路径。 |
| `media/mqsim_wrapper/pymqsim/simulator.py` / `output.py` | native binding 调用、输出文件定位与指标解析。 |
| `run.py` | JSON 驱动的示例/CLI；`--workload/-w` 加载 KV workload JSON，自动分配完整 KV region 后通过 engine 执行。相对配置路径按当前工作目录解析。 |

## KV cache load workload

`workload/kv_cache_load/` 是当前唯一的正式高层 workload。它位于
`MemoryEngine` 之上，只生成 byte address、byte size 和
`MemoryRequestType`，不能直接调用 Ramulator/MQSim wrapper，也不能为了
workload 修改 `media/` 默认行为。

### 数据和 page 语义

- 一个 token 是不可拆分的逻辑对象；`token_size_bytes` 是当前仿真对象范围内
  一个完整 token 的字节数，常见示例为 576 B 或 640 B。当前不建模 token
  内部 component。
- `page_size_tokens` 是一个软件 KV page（例如 vLLM block/SGLang page）中的
  token 数，不是 OS page、DRAM row 或 NAND page。
- 单个完整软件 page 的有效数据量为
  `page_data_bytes = page_size_tokens * token_size_bytes`。
- 相邻软件 page 的地址距离为
  `page_stride_bytes = align_up(page_data_bytes, page_alignment_bytes)`；
  对齐 padding 只影响地址间距，page 粒度请求大小仍为 `page_data_bytes`。
- `KVPageLayout.required_region_size()` 按完整软件 page 分配 context region；
  CLI workload JSON 中 `base_addr` 必须省略或为 0，由 `run.py` 调用
  `MemoryEngine.get_tensor_addr()` 分配。

### Pattern 与加载粒度

| 配置 | 精确语义 |
| --- | --- |
| `CONTIGUOUS` | 从 `start_token` 开始选择连续 `access_tokens` 个 token；`start_token` 只允许用于该 pattern。 |
| `SPARSE_UNIFORM` | 使用 `seed` 从 `[0, context_tokens)` 无放回均匀采样 `access_tokens` 个 token；采样顺序就是 workload 顺序。 |
| `SPARSE_PAGE_LOCAL` | 随机打乱软件 page，再从每个 page 随机选最多 `selected_tokens_per_page` 个 token，直到得到 `access_tokens` 个 token。 |
| `TOKEN` granularity | 每个被选 token 生成一条大小为 `token_size_bytes` 的请求，并保留 selector 顺序。 |
| `PAGE` granularity | 将被选 token 映射到 page，按首次触达顺序对 page ID 去重；每个唯一 page 生成一条大小为 `page_data_bytes` 的请求。 |

workload 层不排序、不合并相邻请求。Page 粒度的首次触达去重是“一个软件
page 只加载一次”的语义，不属于请求合并。请求进入 engine 后，backend 仍可
按自身配置执行转换：Ramulator 拆分 DRAM transaction；MQSim 根据
`mqsim.json` 的 `merge_contiguous` 和 `request_size` 执行 trace 合并与切片。

`GeneratedKVCacheLoad.stats` 的口径：

- `selected_tokens`：selector 产生的 token 数；
- `unique_pages`：这些 token 命中的唯一软件 page 数；
- `logical_requests`：提交到 `MemoryEngine` 的请求数；
- `demand_bytes = selected_tokens * token_size_bytes`；
- `issued_bytes = sum(request sizes)`；
- `page_utilization = selected_tokens / (unique_pages * page_size_tokens)`；
- `read_amplification = issued_bytes / demand_bytes`。

以上都是 workload/engine 入口口径，不等于 Ramulator transaction 数或 MQSim
trace line/sector 数。

### JSON 与 CLI

KV JSON 的必填字段是 `access_tokens`、`context_tokens`、
`token_size_bytes`、`pattern` 和 `granularity`；可选
`workload_type` 固定为 `kv_cache_load`。样例位于
`configs/workloads/`，覆盖三种 pattern 和两种粒度的全部六种组合。

```bash
cd ..
python -m storage_mem_sim.run \
  -c storage_mem_sim/configs/analytic.json \
  -w storage_mem_sim/configs/workloads/kv_sparse_page.json
```

`--workload` 不能与通用负载参数 `--num-requests`、`--size` 同时使用。当前
CLI 明确要求 `storage_instance_num=1`；程序化使用也应维持单 instance 假设，
除非后续任务明确设计多 instance workload 语义。

## 后端选择与分析方法

### 1. 先定义问题和 workload

记录内存类型、读写比例、请求数量、单请求大小、地址连续性/步长、总字节数、DP、实例数、容量和预期并行度。任何对比都应固定这些条件，除非它们本身是实验变量。

### 2. 用 Analytic 建立基线

使用 `T_roof = bytes / peak_bandwidth` 检查单位和数量级。Analytic 把请求串行求和，不表达 bank/channel 并行、排队、row locality、NAND 固有延迟或 host overhead。真实后端明显快于该“峰值带宽”基线，通常意味着字节统计、并行口径或单位有误。

### 3. 选择高精度后端解释差距

- DRAM/HBM 用 Ramulator：关注 transaction 对齐与放大、地址映射、channel/bank 并行、row hit、调度、刷新、控制器最大周期及 YAML 器件 preset。
- SSD/NVMe 用 MQSim：关注 512 B sector/LBA、相邻请求是否合并、trace `request_size` 切片、CWDP 分布、channel/chip/die/plane 几何、读写比例、延迟、IOPS 和带宽 bound 的转换。

### 4. 做不变量和敏感性检查

- `requested_bytes`、backend 实际处理字节和指标分母应一致。
- Ramulator 的 `num_media_reqs` 应反映 transaction 覆盖数；未对齐访问可能多一个 transaction。
- MQSim 的 engine 请求数、合并后 chunk 数和 trace 行数是不同层级，不应混用。
- 请求大小变化时：固定总字节看 IOPS/带宽转换；固定请求数看总负载变化。
- 地址模式变化时：连续、固定 stride、随机、热点分别测试。
- DP/实例变化时同时报告局部与全局请求数，并牢记当前“只模拟一个同构实例”的假设。
- 对高精度结果至少做一次极小 trace 的手算或 direct-backend 对照；Ramulator 已有 wrapper-vs-direct integration tests。

### 5. 报告结论

至少给出配置文件、命令、代码版本、是否构建 native backend、workload 定义、预热/重复方式（如有）、时间/周期/带宽/IOPS、请求计数层级、相对 Analytic 基线的差距，以及该后端没有覆盖的系统成本。不要跨后端直接比较 `cycles`；优先统一到秒和 B/s，并说明时钟来源。

## 开发规范

- Python 目标版本为 3.10+。保持现有 dataclass、类型标注、模块 docstring 和 `logging` 风格；库代码不要新增无条件 `print`，CLI/明确的仿真进度输出除外。
- 公共 API 使用绝对含义清楚的名称，并在 docstring 标注单位。新增配置字段必须同时更新 dataclass、CLI/config 示例、校验和文档。
- 尽早校验无效输入，异常信息包含字段和值。不要静默修正会改变实验语义的配置。
- 不要在 `MemoryEngine` 中用 backend 类型分支实现介质行为。后端差异封装在 `BaseMediaSystem` 子类或 wrapper 内。
- 临时文件使用唯一名称并在 `finally` 中清理；如果产物有意保留用于诊断，路径必须可发现且避免覆盖并发任务。
- 保持指标层级清晰：logical engine request、backend/media request、MQSim trace line 不可混称。新增累计指标时说明应求和、取最大值、加权平均还是重算。
- 修改公式、地址变换、单位或并行语义时必须增加边界测试，并在注释中写出公式与假设，而非只写实现步骤。
- 配置样例应可复现；不要提交机器专属绝对路径。JSON 用于 CLI 包装配置，Ramulator 原生器件配置使用 YAML，MQSim 使用 XML。
- 不手改构建产物或上游 submodule 来绕过适配层问题。确需修改 submodule 时，把它视为独立变更并说明对应上游 commit。

## 测试与验证

在仓库父目录运行包级命令最稳妥：

```bash
cd ..
python -m pytest storage_mem_sim/tests
python -m storage_mem_sim.run -c storage_mem_sim/configs/analytic.json
```

也可在仓库根目录执行 `python -m pytest`；测试使用相对导入，环境需能把仓库作为 package 解析。开发依赖：

```bash
python -m pip install -e '.[dev]'
```

验证策略：

- 纯 engine、配置、metrics、Analytic 或 MQSim trace/XML 逻辑：运行相应单测，最后运行完整 `pytest`。
- KV workload 变更：运行 `python -m pytest tests/workload/kv_cache_load`；修改 selector、layout、单位、page 去重或统计公式时，必须补充对应边界测试。
- KV workload 的 Ramulator/MQSim 看护分别使用 `ramulator_native` 和 `mqsim_native` marker；交付时明确 native 测试是实际运行还是跳过。
- Ramulator 变更：除单元测试外运行 `tests/test_ramulator_integration.py`；未安装 native binding 时相关测试会跳过，交付说明必须明确“跳过”而非“通过”。
- MQSim native/C++ 变更：运行 trace/XML 单测和 native integration tests，确认输出解析，并用小 workload 做一次端到端仿真。
- 文档/配置变更：至少运行 Analytic CLI，并检查文档中的路径、单位和命令与当前代码一致。

Native 后端按需构建，不要为了无关任务重编译：

```bash
git submodule update --init media/ramulator_wrapper/ramulator2
python -m pip install -e media/ramulator_wrapper/ramulator2

git submodule update --init media/mqsim_wrapper/MQSim
python -m pip install -e media/mqsim_wrapper
```

显式运行 KV workload native tests：

```bash
python -m pytest tests/workload/kv_cache_load -m ramulator_native
python -m pytest tests/workload/kv_cache_load -m mqsim_native
```

## 常见陷阱

- 仓库目录名是 `storage_mem_sim`，但发行包名是 `memengine`；源码使用相对导入，示例通常通过 `python -m storage_mem_sim.run` 从父目录运行。
- `MemoryEngineConfig` 的容量字段在 `__post_init__` 派生。构造后再赋 `media_config` 不会自动重算容量。
- 多实例当前只模拟第一个非空分片，适用于同构、均衡、并行实例的代表性估计；不适用于异构实例或尾部不均衡的精确关键路径。
- `MemoryObject.media_req_num` 是按 engine granularity 的估计；Ramulator 的实际覆盖数由对齐后的 transaction 分解决定，MQSim 还会合并和切片。
- Analytic 配置写作 GB/s，但实现按 GiB/s (`1024**3`) 转换；报告时必须指出口径。
- MQSim XML 的 IOPS 是 trace 请求经过 NVMe/PCIe/SSD 路径后的端到端 device IOPS，不是 NAND 内部 page read/program transaction rate；Analytic 和 Ramulator 不提供 IOPS。
- MQSim `handler_mem_request()` 会在 wrapper 的 `trace/` 下保留生成 trace/workload/output；不要把运行产物误当源码提交。
- `configs/mqsim.json` 中的字段不一定都被 CLI 使用；新增或依赖字段前沿 `run.py -> MediaConfig -> backend` 路径确认实际生效。
- `access_tokens` 始终表示 selector 选择的 token 数，不直接表示 page 数。对 `SPARSE_PAGE_LOCAL`，完整 page 条件下预计触达 page 数为 `ceil(access_tokens / selected_tokens_per_page)`。
- `SPARSE_UNIFORM + PAGE` 是“随机 token 后映射到唯一 page”，最终 page 数不是固定配置值；若要可控的随机 page 数，使用 `SPARSE_PAGE_LOCAL + PAGE`。
- `page_data_bytes` 是单个软件 page 的有效数据量；本次 page 请求总字节数是 `unique_pages * page_data_bytes`，不要把它误写成整个 workload 的 page 大小。
- workload 的“不合并”只约束 generator 输出。MQSim 是否继续合并由 `configs/mqsim.json` 的 `merge_contiguous` 决定，trace slice size 由同一配置的 `request_size` 决定。
- KV CLI 状态栏中的 `Trace slice` 是 MQSim backend 参数；实际 KV 请求数以结果区的 `Requests`/`GeneratedKVCacheLoad.stats.logical_requests` 为准。

## 相关文档

- `README.md`：安装和快速运行入口。
- `docs/design.md`：较详细的架构背景；可能落后于实现，使用时与本文件和源码核对。
- `docs/kv_cache_load_workload.md`：KV workload 的 pattern、token/page 粒度、JSON 配置、统计口径和 backend 边界。
- `docs/ramulator_config.md`：Ramulator 配置说明。
- `docs/MQSim后端说明.md`：MQSim 后端总览。
- `docs/trace_generation_analysis.md`、`docs/MQSim_xml说明.md`：trace 生成、地址转换和 XML 专题。
- `docs/mqsim_ssd_guide.md`：SSD 配置与分析指南。

## 修改完成前检查

1. 变更是否尊重 engine/media/wrapper 的职责边界？
2. 单位、请求层级、DP/实例和并行假设是否写清？
3. 是否只改了自研源码，且保留了用户已有变更？
4. 是否覆盖正常、空输入、边界、未对齐和无效配置？
5. 高精度后端测试是真正运行还是因依赖缺失跳过？
6. README、配置示例、设计文档和本文件是否需要同步？
7. 若修改 KV workload，是否仍只通过 `MemoryEngine` 对接 backend，并保持 selector 顺序、page 首次触达去重和统计口径一致？
