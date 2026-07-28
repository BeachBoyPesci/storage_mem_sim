# KV Cache Load Workload

`workload/kv_cache_load/` 在 `MemoryEngine` 之上生成 KV cache 读取请求。
它只输出 byte address、byte size 和 `MemoryRequestType`，不依赖 Analytic、
Ramulator 或 MQSim 后端。

## 支持的语义

访问模式与加载粒度相互独立：

| 访问模式 | Token 粒度 | Page 粒度 |
| --- | --- | --- |
| `CONTIGUOUS` | 每个连续 token 一条请求 | 每个被覆盖的 KV page 一条请求 |
| `SPARSE_UNIFORM` | 从上下文无放回采样 token | 将采样 token 映射到 page，按首次命中去重 |
| `SPARSE_PAGE_LOCAL` | 每个随机 page 选择固定数量 token | 对命中的 page 按首次命中去重 |

一个 token 是不可拆分的逻辑对象。`token_size_bytes`（例如 576 B 或
640 B）应已经包含本次仿真对象范围内的完整 token 数据。

一个 KV page 是包含 `page_size_tokens` 个 token 的软件页，不是 OS page、
DRAM row 或 NAND page。完整 page 的有效数据量为：

```text
page_data_bytes = page_size_tokens * token_size_bytes
```

第一版在 workload 层不排序、不合并请求，并假设
`storage_instance_num=1`。Page 粒度对 page ID 去重是整页加载语义的一部分，
不是相邻请求合并。请求提交到 `MemoryEngine` 后，backend 仍可按自己的既有
配置执行 transaction 分解、trace 合并、切片或对齐；这些行为不由 workload
修改或覆盖。

## 使用

```python
from storage_mem_sim.workload.kv_cache_load import (
    KVAccessPattern,
    KVCacheLoadConfig,
    KVCacheLoadGenerator,
    KVLoadGranularity,
    KVPageLayout,
)

region_size = KVPageLayout.required_region_size(
    context_tokens=131072,
    token_size_bytes=576,
    page_size_tokens=16,
)
base_addr = engine.get_tensor_addr(region_size)

config = KVCacheLoadConfig(
    access_tokens=2048,
    context_tokens=131072,
    token_size_bytes=576,
    pattern=KVAccessPattern.SPARSE_PAGE_LOCAL,
    granularity=KVLoadGranularity.PAGE,
    page_size_tokens=16,
    selected_tokens_per_page=4,
    base_addr=base_addr,
    seed=42,
)

generated = KVCacheLoadGenerator().generate(config)
metrics = generated.issue(engine)
```

## JSON 配置与 CLI

KV workload 和 backend 配置相互独立。同一个 workload JSON 可以通过替换
`--config` 分别运行在 Analytic、Ramulator 和 MQSim 上：

```bash
python -m storage_mem_sim.run \
  --config storage_mem_sim/configs/analytic.json \
  --workload storage_mem_sim/configs/workloads/kv_sparse_page.json
```

示例 workload：

```json
{
  "workload_type": "kv_cache_load",
  "context_tokens": 4096,
  "access_tokens": 128,
  "token_size_bytes": 576,
  "pattern": "sparse_page_local",
  "granularity": "page",
  "page_size_tokens": 16,
  "selected_tokens_per_page": 4,
  "page_alignment_bytes": 1,
  "seed": 42
}
```

这个配置随机选择 32 个 page，每个 page 用 4 个 token 表达需求，最终生成
32 条、每条 9216 B 的整页请求。CLI 通过 `MemoryEngine.get_tensor_addr()`
为完整 KV region 分配 `base_addr`，因此 CLI workload JSON 中应省略
`base_addr` 或将其设为 0。

仓库提供了完整的访问模式和请求粒度组合：

| 配置文件 | Pattern | Granularity | 当前示例产生的请求 |
| --- | --- | --- | --- |
| `kv_contiguous_token.json` | `contiguous` | `token` | 128 条 token 请求 |
| `kv_contiguous_page.json` | `contiguous` | `page` | 8 条 page 请求 |
| `kv_sparse_uniform_token.json` | `sparse_uniform` | `token` | 128 条 token 请求 |
| `kv_sparse_uniform_page.json` | `sparse_uniform` | `page` | 随机 token 命中的唯一 page 请求 |
| `kv_sparse_page_local_token.json` | `sparse_page_local` | `token` | 32 个随机 page 中的 128 条 token 请求 |
| `kv_sparse_page.json` | `sparse_page_local` | `page` | 32 条随机 page 请求 |

这些配置都位于 `configs/workloads/`，并使用相同的 context、token size 和
访问 token 数，便于只改变 pattern 或 granularity 做对照实验。

`--workload` 不能与通用负载的 `--num-requests`、`--size` 同时使用。未传
`--workload` 时，CLI 保持原有的连续等长请求行为。

`selected_tokens_per_page` 只用于 `SPARSE_PAGE_LOCAL`，表示一个被选中 KV
page 内最多选择多少 token。它控制合成稀疏负载的空间局部性：

```text
预计 unique pages ≈ ceil(access_tokens / selected_tokens_per_page)
预计 page 读取放大 ≈ page_size_tokens / selected_tokens_per_page
```

## 统计口径

`GeneratedKVCacheLoad.stats` 包含：

- `demand_bytes`：被选择 token 的有效数据量；
- `issued_bytes`：提交给 `MemoryEngine` 的字节数；
- `unique_pages`：被选择 token 命中的 KV page 数；
- `page_utilization`：被选择 token 占所加载 page token slots 的比例；
- `read_amplification`：`issued_bytes / demand_bytes`。

Token 粒度通常没有 workload 层读取放大。Page 粒度会因为整页加载而增加
`issued_bytes`。

## DSA KV 加载 HBM 对比实验

本节记录 A2、A3、A5 950PR 和 A5 950DT HBM 近似配置上的 DSA KV
加载实验。实验于 2026-07-24 使用 Ramulator native binding 实际运行；
DDR 配置尚待校准，不包含在本节结论中。

### HBM 配置与统一假设

| 平台 | Ramulator 配置 | 近似模型 | Controller | Mapper | Tick 频率 |
| --- | --- | --- | ---: | --- | ---: |
| A2 | `configs/ramulator/ascend_a2_hbm.yaml` | HBM2e-3200 | 64 | `RoBaRaCoCh` | 1600 MHz |
| A3 | `configs/ramulator/ascend_a3_hbm.yaml` | 复用 A2 HBM2e-3200 | 64 | `RoBaRaCoCh` | 1600 MHz |
| A5 950PR | `configs/ramulator/ascend_a5_950pr_hbm.yaml` | HBM3-6400 | 32 | `MOP4CLXOR` | 3205.128 MHz |
| A5 950DT | `configs/ramulator/ascend_a5_950dt_hbm.yaml` | HBM3e-8000 近似 | 64 | `MOP4CLXOR` | 4000 MHz |

所有实验统一使用：

```text
access_tokens       = 2048
context_tokens      = 131072
token_size_bytes    = 576
request_type        = KREAD
page_alignment_bytes = 1
seed                = 42（主结果；token 敏感性检查另行覆盖）
DP                  = 1
storage instances   = 1
Ramulator tx_bytes  = 32
```

HBM 配置均使用 `NoRefresh`。当前 `LoadStoreTrace` frontend 会尽快向
controller 队列注入请求，因此这些结果表达的是深 outstanding request
窗口下的介质服务时间，不包含有限 issue width、上层依赖、DMA/gather
开销、cache/TLB 或片间链路成本。

从仓库根目录运行实验：

```bash
python run.py \
  --config configs/ramulator/ascend_a2_hbm.json \
  --workload <workload.json>
```

将 `--config` 依次替换为：

```text
configs/ramulator/ascend_a2_hbm.json
configs/ramulator/ascend_a3_hbm.json
configs/ramulator/ascend_a5_950pr_hbm.json
configs/ramulator/ascend_a5_950dt_hbm.json
```

### Token 粒度：连续与均匀稀疏

连续 workload 配置：

```json
{
  "workload_type": "kv_cache_load",
  "context_tokens": 131072,
  "access_tokens": 2048,
  "token_size_bytes": 576,
  "pattern": "contiguous",
  "granularity": "token",
  "page_size_tokens": 16,
  "page_alignment_bytes": 1,
  "start_token": 0
}
```

均匀稀疏 workload 配置：

```json
{
  "workload_type": "kv_cache_load",
  "context_tokens": 131072,
  "access_tokens": 2048,
  "token_size_bytes": 576,
  "pattern": "sparse_uniform",
  "granularity": "token",
  "page_size_tokens": 16,
  "page_alignment_bytes": 1,
  "seed": 42
}
```

两种 pattern 都生成 2048 条逻辑请求，需求和发出字节均为
`2048 * 576 = 1,179,648 B`。因为 576 B 能被 32 B transaction 整除，
两者都生成 36,864 个 Ramulator transaction，没有 transaction 字节放大。

Seed 42 的结果：

| 平台 | Pattern | Cycles | 时间 | 实际发出带宽 |
| --- | --- | ---: | ---: | ---: |
| A2 | 连续 | 1268 | 792.5 ns | 1488.51 GB/s |
| A2 | 均匀稀疏 | 4543 | 2839.4 ns | 415.46 GB/s |
| A3 | 连续 | 1268 | 792.5 ns | 1488.51 GB/s |
| A3 | 均匀稀疏 | 4543 | 2839.4 ns | 415.46 GB/s |
| A5 950PR | 连续 | 2933 | 915.1 ns | 1289.10 GB/s |
| A5 950PR | 均匀稀疏 | 7357 | 2295.4 ns | 513.92 GB/s |
| A5 950DT | 连续 | 1531 | 382.8 ns | 3082.03 GB/s |
| A5 950DT | 均匀稀疏 | 5111 | 1277.8 ns | 923.22 GB/s |

使用 seeds `1、7、42、123、2025` 做稀疏访问敏感性检查：

| 平台 | 连续时间 | 稀疏平均时间 | 稀疏时间范围 | 稀疏平均带宽 | 连续加速比 |
| --- | ---: | ---: | ---: | ---: | ---: |
| A2 | 792.5 ns | 2881.4 ns | 2834.4–2956.9 ns | 409.54 GB/s | 3.64× |
| A3 | 792.5 ns | 2881.4 ns | 2834.4–2956.9 ns | 409.54 GB/s | 3.64× |
| A5 950PR | 915.1 ns | 2293.9 ns | 2275.4–2331.6 ns | 514.30 GB/s | 2.51× |
| A5 950DT | 382.8 ns | 1291.6 ns | 1270.8–1328.8 ns | 913.60 GB/s | 3.37× |

A3 当前复用 A2 的全部 HBM 组织和时序参数，因此结果相同是配置决定的，
不能解释为 A2 与 A3 实机性能完全相同。

### Page 粒度：32-token page 与不同页内命中率

Page 实验使用 `SPARSE_PAGE_LOCAL`。每个软件 page 包含 32 个 token：

```text
page_data_bytes = 32 * 576 = 18,432 B
```

配置模板如下，`selected_tokens_per_page` 分别取 `1、2、4、8、16`：

```json
{
  "workload_type": "kv_cache_load",
  "context_tokens": 131072,
  "access_tokens": 2048,
  "token_size_bytes": 576,
  "pattern": "sparse_page_local",
  "granularity": "page",
  "page_size_tokens": 32,
  "selected_tokens_per_page": 4,
  "page_alignment_bytes": 1,
  "seed": 42
}
```

`access_tokens` 始终为需求 token 数，不是 page 数。由于所有 page 都是完整
page，触达 page 数和读取放大为：

| 每页命中 token | Page 利用率 | 触达 page | 逻辑请求 | 发出字节 | 读放大 | 32 B transaction |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 3.125% | 2048 | 2048 | 37,748,736 B | 32× | 1,179,648 |
| 2 | 6.25% | 1024 | 1024 | 18,874,368 B | 16× | 589,824 |
| 4 | 12.5% | 512 | 512 | 9,437,184 B | 8× | 294,912 |
| 8 | 25% | 256 | 256 | 4,718,592 B | 4× | 147,456 |
| 16 | 50% | 128 | 128 | 2,359,296 B | 2× | 73,728 |

实际完成时间：

| 每页命中 token | A2/A3 | A5 950PR | A5 950DT |
| ---: | ---: | ---: | ---: |
| 1 | 28.768 µs | 39.433 µs | 22.858 µs |
| 2 | 14.214 µs | 19.446 µs | 11.398 µs |
| 4 | 7.185 µs | 9.768 µs | 5.715 µs |
| 8 | 3.616 µs | 4.941 µs | 2.921 µs |
| 16 | 1.789 µs | 2.392 µs | 1.479 µs |

Ramulator/CLI 的带宽使用 `issued_bytes / time`，表示 HBM 实际传输整页数据
的介质带宽：

| 每页命中 token | A2/A3 | A5 950PR | A5 950DT |
| ---: | ---: | ---: | ---: |
| 1 | 1312.17 GB/s | 957.28 GB/s | 1651.43 GB/s |
| 2 | 1327.90 GB/s | 970.60 GB/s | 1655.97 GB/s |
| 4 | 1313.46 GB/s | 966.15 GB/s | 1651.23 GB/s |
| 8 | 1304.83 GB/s | 954.96 GB/s | 1615.54 GB/s |
| 16 | 1318.96 GB/s | 986.28 GB/s | 1595.47 GB/s |

为了表达 DSA 上层真正需要的 2048 个 token，还需要使用
`demand_bytes / time` 计算有效需求带宽：

| 每页命中 token | A2/A3 | A5 950PR | A5 950DT |
| ---: | ---: | ---: | ---: |
| 1 | 41.01 GB/s | 29.91 GB/s | 51.61 GB/s |
| 2 | 82.99 GB/s | 60.66 GB/s | 103.50 GB/s |
| 4 | 164.18 GB/s | 120.77 GB/s | 206.40 GB/s |
| 8 | 326.21 GB/s | 238.74 GB/s | 403.89 GB/s |
| 16 | 659.48 GB/s | 493.14 GB/s | 797.73 GB/s |

作为连续 page 基线，`page_size_tokens=32` 时连续 2048 个 token 覆盖 64
个 page，发出字节仍为 1,179,648 B：

| 平台 | 连续时间 | 连续带宽 |
| --- | ---: | ---: |
| A2/A3 | 792.5 ns | 1488.51 GB/s |
| A5 950PR | 915.1 ns | 1289.10 GB/s |
| A5 950DT | 382.8 ns | 3082.03 GB/s |

不同页内命中数的稀疏 page 加载相对连续 page 的时间倍数：

| 每页命中 token | A2/A3 | A5 950PR | A5 950DT |
| ---: | ---: | ---: | ---: |
| 1 | 36.30× | 43.09× | 59.72× |
| 2 | 17.94× | 21.25× | 29.78× |
| 4 | 9.07× | 10.67× | 14.93× |
| 8 | 4.56× | 5.40× | 7.63× |
| 16 | 2.26× | 2.61× | 3.86× |

结果表明，实际介质带宽在同一平台的不同命中率下相对稳定，完成时间主要
由整页读取放大决定。每页命中 token 数翻倍时，触达 page 数、发出字节和
完成时间均近似减半。即使每页命中 16 个 token、page 利用率达到 50%，仍
存在 2× 读取放大和随机 page 顺序损失，因此明显慢于连续访问。

## DSA KV 加载 DDR 对比实验

本节使用与 HBM 实验相同的 DSA KV workload，对比 32-channel DDR4-3200
host 近似配置和 32-channel DDR5 MRDIMM-8800 host-side 带宽等效配置。
实验于 2026-07-25 使用 Ramulator native binding 实际运行。

### DDR 配置与统一假设

| 配置 | Ramulator 配置 | 物理 channel | Controller | 容量 | 目标峰值带宽 | Tick 频率 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| DDR4-3200 | `configs/ramulator/host_ddr4_2tb_819p2gb_per_s_3200.yaml` | 32 × 64 bit | 32 | 2 TiB | 819.2 GB/s | 1600 MHz |
| MRDIMM-8800 近似 | `configs/ramulator/host_ddr5_mrdimm_4tb_2252p8gb_per_s_8800.yaml` | 32 × 64 bit | 64 × 32 bit | 4 TiB | 2252.8 GB/s | 4405.286 MHz |

DDR4 controller 表示一个完整 64-bit channel；DDR5 DIMM channel 被拆成
两个独立 32-bit subchannel，因此 32 个插槽使用 64 个 controller。
MRDIMM 的 2252.8 GB/s 是十进制 `GB/s`：

```text
32 channel * 64 bit / 8 * 8800e6 = 2252.8e9 B/s = 2252.8 GB/s
2252.8e9 B/s / 1024^3 = 2098.08 GiB/s = 2.049 TiB/s
```

因此它是约 2.25 TB/s 或 2.05 TiB/s，不能只写成没有单位口径的“2T”。
由于 Ramulator 配置使用整数 `tCK_ps=227`，实际 tick 为 4405.286 MHz，
比目标 4400 MHz 高约 0.12%。

MRDIMM 配置使用 `DDR5_32Gb_x4`、每个 subchannel 两个 rank：

```text
32 Gb/device * 8 x4 devices/subchannel * 2 ranks
  = 64 GiB/subchannel
2 subchannels/DIMM * 64 GiB = 128 GiB/DIMM
32 DIMMs * 128 GiB = 4 TiB
```

当前 Ramulator2 没有 MRDIMM multiplexer、buffer 和内部 rank 聚合模型。
该配置把 MRDIMM 表示成 host-visible 8800 MT/s 的普通双 rank DDR5，
并按 DDR5-6400AN 的绝对时间缩放主要时序参数。因此本节结果适合比较主机侧
带宽、地址分布和读取放大，不是 MRDIMM 周期精确模型。

两种 DDR 配置均使用：

```text
access_tokens        = 2048
context_tokens       = 131072
token_size_bytes     = 576
request_type         = KREAD
page_alignment_bytes = 1
seed                 = 42（主结果；token 敏感性检查另行覆盖）
DP                   = 1
storage instances    = 1
Ramulator tx_bytes   = 64
RefreshManager       = AllBank
```

这与 HBM 章节的 `NoRefresh` 假设不同，因此 DDR 与 HBM 表格不能直接解释
成只由介质类型造成的差异。`LoadStoreTrace` 仍尽快注入请求，结果表达深
outstanding request 窗口下的 DRAM 服务时间，不包含 NUMA 远端访问、
CPU 间互连、DMA/gather、cache/TLB 或 MRDIMM buffer 的额外系统成本。

从仓库根目录运行：

```bash
python run.py \
  --config configs/ramulator/host_ddr4_2tb_819p2gb_per_s_3200.json \
  --workload <workload.json>

python run.py \
  --config configs/ramulator/host_ddr5_mrdimm_4tb_2252p8gb_per_s_8800.json \
  --workload <workload.json>
```

### Token 粒度：连续与均匀稀疏

本实验复用 HBM 章节的连续 token 和均匀稀疏 token JSON。两种 pattern
均生成 2048 条逻辑请求，需求和发出字节都是 1,179,648 B。576 B 能被
64 B transaction 整除，因此均生成 18,432 个 Ramulator transaction，
没有 transaction 字节放大。

Seed 42 的结果：

| 配置 | Pattern | Cycles | 时间 | 实际发出带宽 |
| --- | --- | ---: | ---: | ---: |
| DDR4-3200 | 连续 | 4181 | 2.613 µs | 451.43 GB/s |
| DDR4-3200 | 均匀稀疏 | 2810 | 1.756 µs | 671.69 GB/s |
| MRDIMM-8800 近似 | 连续 | 5175 | 1.175 µs | 1004.19 GB/s |
| MRDIMM-8800 近似 | 均匀稀疏 | 3193 | 724.8 ns | 1627.52 GB/s |

使用 seeds `1、7、42、123、2025` 做均匀稀疏访问敏感性检查：

| 配置 | 连续时间 | 稀疏平均时间 | 稀疏时间范围 | 稀疏平均带宽 | 稀疏/连续时间比 |
| --- | ---: | ---: | ---: | ---: | ---: |
| DDR4-3200 | 2.613 µs | 1.781 µs | 1.741–1.836 µs | 662.26 GB/s | 0.68× |
| MRDIMM-8800 近似 | 1.175 µs | 717.0 ns | 684.6–741.4 ns | 1645.35 GB/s | 0.61× |

与 HBM 结果不同，当前 DDR 配置中的均匀稀疏 token 访问快于连续访问。
在 `CacheLineInterleave + RoBaRaCoCh` 映射下，连续 transaction 在每个
channel 内更集中于相同 bank group，受到 same-bank-group column timing
约束；随机 token 的 9 个连续 transaction 则随 token 地址分散到更多
bank/bank group，暴露出更多并行度。这个结果依赖当前地址映射、队列深度和
时序配置，不能泛化为“真实 DDR 随机访问总是快于连续访问”。

MRDIMM-8800 近似相对 DDR4-3200 的完成时间加速为：

| Pattern | DDR4 时间 | MRDIMM 时间 | MRDIMM 加速 |
| --- | ---: | ---: | ---: |
| 连续 | 2.613 µs | 1.175 µs | 2.22× |
| 均匀稀疏五 seed 平均 | 1.781 µs | 717.0 ns | 2.48× |

### Page 粒度：32-token page 与不同页内命中率

Page workload 与 HBM 章节完全相同：`page_size_tokens=32`，
`selected_tokens_per_page` 取 `1、2、4、8、16`，seed 为 42。
单页有效数据仍为 18,432 B，但 DDR transaction 是 64 B：

| 每页命中 token | Page 利用率 | 触达 page | 逻辑请求 | 发出字节 | 读放大 | 64 B transaction |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 3.125% | 2048 | 2048 | 37,748,736 B | 32× | 589,824 |
| 2 | 6.25% | 1024 | 1024 | 18,874,368 B | 16× | 294,912 |
| 4 | 12.5% | 512 | 512 | 9,437,184 B | 8× | 147,456 |
| 8 | 25% | 256 | 256 | 4,718,592 B | 4× | 73,728 |
| 16 | 50% | 128 | 128 | 2,359,296 B | 2× | 36,864 |

实际完成时间：

| 每页命中 token | DDR4-3200 | MRDIMM-8800 近似 | MRDIMM 加速 |
| ---: | ---: | ---: | ---: |
| 1 | 52.858 µs | 19.102 µs | 2.77× |
| 2 | 26.567 µs | 9.583 µs | 2.77× |
| 4 | 12.947 µs | 4.805 µs | 2.69× |
| 8 | 6.174 µs | 2.198 µs | 2.81× |
| 16 | 3.151 µs | 1.162 µs | 2.71× |

Ramulator/CLI 的带宽为 `issued_bytes / time`，表示实际传输整页数据的介质
带宽：

| 每页命中 token | DDR4-3200 | MRDIMM-8800 近似 |
| ---: | ---: | ---: |
| 1 | 714.15 GB/s | 1976.16 GB/s |
| 2 | 710.45 GB/s | 1969.61 GB/s |
| 4 | 728.88 GB/s | 1964.16 GB/s |
| 8 | 764.22 GB/s | 2146.95 GB/s |
| 16 | 748.83 GB/s | 2030.35 GB/s |

使用 `demand_bytes / time` 得到 DSA 真正需要的 2048 个 token 的有效需求
带宽：

| 每页命中 token | DDR4-3200 | MRDIMM-8800 近似 |
| ---: | ---: | ---: |
| 1 | 22.32 GB/s | 61.76 GB/s |
| 2 | 44.40 GB/s | 123.10 GB/s |
| 4 | 91.11 GB/s | 245.52 GB/s |
| 8 | 191.06 GB/s | 536.74 GB/s |
| 16 | 374.42 GB/s | 1015.18 GB/s |

连续 page 基线覆盖 64 个 page，生成的 18,432 个 transaction 与连续
token 基线相同，因此虽然逻辑请求数从 2048 降为 64，两个 backend 的完成
时间和带宽都与连续 token 基线相同：

| 配置 | 连续 page 时间 | 连续 page 带宽 |
| --- | ---: | ---: |
| DDR4-3200 | 2.613 µs | 451.43 GB/s |
| MRDIMM-8800 近似 | 1.175 µs | 1004.19 GB/s |

Page 稀疏实验传输的数据量较大，能更充分填满 controller 队列。DDR4 实际
介质带宽为 710–764 GB/s，MRDIMM 近似为 1964–2147 GB/s。MRDIMM 的理论
带宽是 DDR4 的 2.75×，Page 完成时间加速为 2.69–2.81×，两者基本一致。
相较之下，短得多的连续 token/page 基线只达到 451 GB/s 和 1004 GB/s，
不能用来代表两个配置的饱和峰值带宽。

与 HBM 实验相同，page 利用率仍是主导有效 DSA 性能的因素。即使
MRDIMM-8800 近似的介质带宽达到约 2 TB/s，每页只命中 1 个 token 时，
32× 读取放大仍把有效需求带宽压低到 61.76 GB/s；把每页命中数提高到 16
后，有效需求带宽才提高到 1015.18 GB/s。

## 后端边界

- Analytic 使用 `issued_bytes / configured bandwidth`，不表达地址差异。
- Ramulator 如何分解 DRAM transaction，由现有 Ramulator backend 决定。
- MQSim 是否合并、如何切片和转换 sector，由现有 MQSim backend 配置与实现
  决定。

workload 只调用 `MemoryEngine.issue_request(addresses, sizes,
request_types)`，不导入 backend 请求转换逻辑，也不修改 media 配置。这里的
`issued_bytes` 是 workload 提交给 `MemoryEngine` 的逻辑字节数，不等价于
backend 最终产生的 DRAM transaction 或 SSD sector 字节数。

Ramulator 和 MQSim 的 native integration tests 位于
`tests/workload/kv_cache_load/`。缺少 native binding 时测试会明确跳过；
安装相应 binding 后应实际执行。
