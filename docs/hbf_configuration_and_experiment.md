# HBF MQSim 配置与 KV 稀疏加载实验

本文记录当前仓库中 HBF-like MQSim 配置的建模思路、适用边界，以及
KV cache 单 batch 和 128 batch 并发加载实验。这里的 HBF 是为了研究
`16 KiB` 读取下约 `12 M IOPS / 200 GB/s` 的**性能代理**，不是对某款
量产 HBF 器件内部结构的完整复刻。

实验于 2026-07-29 使用 MQSim native binding 实际运行。基础代码版本为
`9150389709c5`，配置和本文档是工作区中的未提交变更。除非特别说明，带宽
统一使用十进制 `GB/s = 10^9 B/s`。

相关文件：

- Engine 配置：`configs/mqsim_hbf_slc_6die_32plane_proxy.json`
- SSD 配置：`configs/mqsim/hbf_slc_6die_32plane_proxy_ssdconfig.xml`
- Workload 模板：`configs/mqsim/hbf_slc_6die_32plane_proxy_workload.xml`
- KV workload 语义：`docs/kv_cache_load_workload.md`

## 1. 建模目标和参考边界

当前目标为：

```text
介质类型       = SLC NAND
目标 IOPS      ≈ 12 M read IOPS
目标带宽       ≈ 200 GB/s
目标 IO size   = 16 KiB
```

IOPS 和带宽满足：

```text
12,000,000 IO/s × 16,384 B/IO = 196.608 GB/s
```

配置思路参考 Sandisk 专利
[US20250259685A1](https://patents.google.com/patent/US20250259685A1)
中的 HBF 实施例：

- SLC operation；
- 每 die 大量可独立并行读取的 plane，参考值为 32 planes/die；
- 约 `15 us` 的 array read latency；
- `16 KiB` logical read unit，专利示例中可由两个 `8 KiB` physical page
  组成。

这些专利参数是架构参考，不应解释成特定量产产品已经确认的内部 BOM。
当前 MQSim 配置也没有实现 subplane，因此把 physical/logical page 的关系
折叠成一个有效的 `16 KiB` MQSim page。

## 2. 为什么不能直接配置 32 planes/die

Stock MQSim 的 XML 可以写入：

```xml
<Plane_No_Per_Die>32</Plane_No_Per_Die>
```

但这不能忠实表达 HBF 的 32 个独立 plane：

1. MQSim 的 die 是主要 busy/scheduling 单元，同一 die 内的 transaction
   不能自然表现为 32 路完全独立 array read。
2. 多 plane command timing 只为有限的 1–4 plane 操作准备，不适合直接扩展
   到 32-plane command。
3. HBF plane 独立并行和 MQSim multi-plane command 是不同语义。前者是多个
   array execution slot，后者通常是一条组合 NAND command。
4. MQSim 没有专利中的 subplane 维度。

原生实验还验证了另一种直观映射：

```text
1 channel × 1 chip × 32 dies/chip
```

在当前 MQSim 调度和 PHY 实现中仍表现出明显串行化，带宽只有约
`6.35 GB/s`，不能代表 32 个独立 plane。

因此当前代理保持：

```xml
<Plane_No_Per_Die>1</Plane_No_Per_Die>
```

并用多个可独立调度的单 die chip 代理 plane。

## 3. 当前 HBF 代理拓扑

映射关系为：

```text
1 个真实 HBF die
  ≈ 1 个 MQSim channel

1 个真实 HBF plane
  ≈ channel 下 1 个 MQSim chip
    × 1 die/chip
    × 1 plane/die
```

实际 XML 拓扑：

```text
6 channels
× 32 chips/channel
× 1 die/chip
× 1 plane/die
= 192 MQSim surrogate dies
= 192 independent array execution slots
```

这里的 192 个 MQSim die **不表示真实 HBF 有 192 个物理 die**。它们代理的
是：

```text
6 个 HBF die × 32 个独立 plane/die
```

同一 channel 下的 32 个代理 chip 共享该 channel 的数据传输资源，因此没有
把 192 路阵列执行错误地建模为 192 条完全独立总线。

主要参数如下：

| 参数 | 当前值 | 建模含义 |
| --- | ---: | --- |
| `Flash_Channel_Count` | 6 | 6 个 HBF die proxy |
| `Chip_No_Per_Channel` | 32 | 每个 HBF die 的 32 个 plane proxy |
| `Die_No_Per_Chip` | 1 | 每个 proxy chip 只有一个执行 die |
| `Plane_No_Per_Die` | 1 | 避免 MQSim multi-plane command 语义 |
| `Flash_Technology` | SLC | HBF SLC operation |
| `Page_Capacity` | 16 KiB | 有效 logical read unit |
| `Page_Read_Latency_LSB` | 15 us | SLC array read latency |
| `Plane_Allocation_Scheme` | CWDP | 连续页依次跨 channel、chip、die、plane |
| Scheduler | `PRIORITY_OUT_OF_ORDER` | 跨 proxy chip 重叠 array work |

### 3.1 理论阵列上界

忽略通道、Host 和 command overhead：

```text
IOPS_array = 192 / 15 us
           = 12.8 M IOPS

BW_array   = 12.8 M × 16 KiB
           = 209.715 GB/s
```

使用 32,768 条对齐的 `16 KiB` 读取、所有请求在 `t=0` 到达时，native
MQSim 结果为：

```text
完成请求       = 32,768
IOPS           = 12.3618 M
带宽           = 202.535 GB/s
```

这个结果是深 outstanding window 下的饱和 batch 吞吐，不是外部按照
12 M IOPS 匀速注入的开放队列结果。

### 3.2 通道参数是有效校准值

```xml
<Flash_Channel_Width>128</Flash_Channel_Width>
<Channel_Transfer_Rate>1000</Channel_Transfer_Rate>
```

`128 B` channel width 不是对 HBF 物理 pin count 的声明。Stock MQSim：

- 对每个 proxy chip 分别产生 command/setup 开销；
- 使用整数 ns 时序，不能直接表达 `4.8 GT/s` 的亚纳秒周期；
- 仍会模拟同一 channel 下 32 个 proxy chip 的共享 data-out 竞争。

因此 channel width 是为了补偿代理映射引入的建模开销，使最终瓶颈接近
`15 us` array bound 的有效参数。

## 4. Host、PCIe、NVMe 和 FTL

当前 Python MQSim wrapper 仍使用 NVMe Host path，没有切换到 DIRECT：

```text
Trace IO flow
  → NVMe SQ/CQ
  → PCIe
  → SSD Host Interface
  → data cache / FTL
  → TSU / PHY / NAND
```

Host 和 NVMe 配置如下：

| 参数 | 当前值 | 含义 |
| --- | ---: | --- |
| `PCIe_Lane_Bandwidth` | 64 GB/s/lane | 人工非瓶颈值 |
| `PCIe_Lane_Count` | 4 | 总等效带宽 256 GB/s |
| `HostInterface_Type` | NVME | 保留 MQSim Host path |
| `IO_Queue_Depth` | 4096 | SQ/CQ 环形队列深度 |
| `Queue_Fetch_Size` | 1024 | 每个 stream 最多 1024 个 on-the-fly 请求 |
| IO flow 数 | 1 | 一个 trace-based flow、一对 SQ/CQ |
| Priority | URGENT | workload priority |

`4 × 64 GB/s = 256 GB/s` 不对应真实 PCIe x4 规格，而是让 Host transport
高于约 `200 GB/s` 的介质目标。当前输出的 IOPS 仍是经过 NVMe/PCIe 路径的
端到端 device IOPS。

Workload 模板还设置：

```xml
<Device_Level_Data_Caching_Mode>TURNED_OFF</Device_Level_Data_Caching_Mode>
<Ideal_Mapping_Table>true</Ideal_Mapping_Table>
<Initial_Occupancy_Percentage>0</Initial_Occupancy_Percentage>
```

因此实验不依靠 data-cache hit，不包含 mapping-table miss 引起的额外 flash
访问，也没有预条件化和 GC 干扰。它主要表达读路径上界。

### 4.1 容量口径

当前容量并非性能目标的一部分：

```text
MemoryEngine 对上层容量 = 40 GiB
MQSim raw geometry       = 48 GiB
扣除 7% OP              ≈ 44.64 GiB
```

如果要得到至少 `100 GiB` 的上层可用容量，同时保持性能拓扑不变，可以将：

```text
capacity                 = 100.0 GiB
Block_No_Per_Plane       = 144
```

此时 raw capacity 为 `108 GiB`，扣除 7% OP 后约 `100.44 GiB`。不建议通过
增加 channel、chip、die 或 plane 来扩容，因为这些参数同时会改变并行度和
性能上界。

## 5. 请求变换和指标口径

当前 Engine 配置为：

```json
{
  "merge_contiguous": true,
  "request_size": 16384,
  "dp": 1,
  "instances": 1
}
```

MQSim trace pipeline 为：

```text
MemoryEngine logical requests
  → 合并请求列表中相邻、同类型、地址连续的请求
  → 将地址范围扩展到 512 B sector 边界
  → 按最大 16 KiB 切分
  → 所有 trace line 的 arrival time 写为 0
```

需要区分三个请求层级：

1. `logical requests`：KV generator 交给 MemoryEngine 的请求数；
2. `trace requests`：合并、sector 对齐和 16 KiB 切分后的 MQSim IO 数；
3. NAND transaction：MQSim 内部生成的介质操作。

本文使用两个带宽口径：

```text
native/media bandwidth
  = MQSim trace bytes / simulation time

effective demand bandwidth
  = selected token demand bytes / simulation time
```

Page 加载可能有 workload read amplification；小 token 访问还可能有
512 B sector amplification。高 media bandwidth 不一定等于高有效 KV
带宽。

## 6. 单 batch KV 稀疏加载

这里“单并发”表示 `batch_count=1`：一个 KV sequence 选择 2048 个 token，
生成的全部请求在一次 `MemoryEngine.issue_request()` 中提交。它不是
`storage_instance_num=1` 和 `128` 的对比；全部实验始终使用一个 storage
instance。

统一参数：

```text
access_tokens          = 2048
context_tokens         = 131072
token_size_bytes       = 576
request_type           = KREAD
page_alignment_bytes   = 1
DP                     = 1
storage instances      = 1
MQSim trace slice      = 16 KiB
merge_contiguous       = true
```

需求数据量固定为：

```text
2048 × 576 B = 1,179,648 B
```

### 6.1 Token 粒度：连续和均匀稀疏

连续 token 是控制组。2048 条连续的 `576 B` logical request 被完整合并后：

```text
1,179,648 B / 16 KiB = 72 条 16 KiB trace IO
```

结果为：

| Pattern | Logical req | Trace req | 时间 | Native BW | 有效需求 BW | IOPS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Contiguous token | 2,048 | 72 | 21.054 us | 56.030 GB/s | 56.030 GB/s | 3.420 M |

均匀稀疏 token 使用五个 seed：

| Seed | Trace req | 时间 | Native BW | 有效需求 BW | IOPS |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 2,048 | 296.660 us | 7.069 GB/s | 3.976 GB/s | 6.904 M |
| 7 | 2,048 | 340.278 us | 6.163 GB/s | 3.467 GB/s | 6.019 M |
| 42 | 2,048 | 313.214 us | 6.696 GB/s | 3.766 GB/s | 6.539 M |
| 123 | 2,048 | 342.952 us | 6.115 GB/s | 3.440 GB/s | 5.972 M |
| 2025 | 2,048 | 312.320 us | 6.715 GB/s | 3.777 GB/s | 6.557 M |
| 平均 | 2,048 | 321.085 us | 6.552 GB/s | 3.685 GB/s | 6.398 M |

稀疏 token 的 workload read amplification 仍是 `1×`，因为 generator
只发出被选 token 的 576 B。但随机请求在 request list 中几乎不连续，
`merge_contiguous=true` 无法合并它们；每条 576 B 范围被扩展为两个
512 B sector：

```text
backend bytes          = 2048 × 1024 B = 2,097,152 B
backend byte amp       = 1024 / 576
                       = 1.7778×
```

因此 native bandwidth 约为有效需求带宽的 `1.7778×`。单 batch 只有
2048 个小 IO，平均只达到约 `6.40 M IOPS`，没有达到 12 M IOPS 饱和点。

当前 `merge=true` 下连续控制组比稀疏平均快约 `15.25×`。这一结论依赖于
请求列表按 token 地址顺序排列；如果跨 batch 交错提交 token，连续地址也
不会被当前预处理器合并。

### 6.2 Page 粒度：稀疏 page-local

Page 实验使用：

```text
page_size_tokens = 32
page_data_bytes   = 32 × 576 B
                  = 18,432 B
```

`selected_tokens_per_page` 分别为 `1、2、4、8、16`。每个被触达软件 page
生成一条 `18,432 B` logical request。单个孤立软件 page 通常切成：

```text
16 KiB + 2 KiB = 2 条 trace IO
```

结果：

| 每页选中 token | Unique pages | Workload放大 | Trace req | 时间 | Native BW | 有效需求 BW | IOPS |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 2,048 | 32× | 4,095 | 670.284 us | 56.318 GB/s | 1.760 GB/s | 6.109 M |
| 2 | 1,024 | 16× | 2,048 | 432.576 us | 43.632 GB/s | 2.727 GB/s | 4.734 M |
| 4 | 512 | 8× | 1,024 | 250.917 us | 37.611 GB/s | 4.701 GB/s | 4.081 M |
| 8 | 256 | 4× | 512 | 163.483 us | 28.863 GB/s | 7.216 GB/s | 3.132 M |
| 16 | 128 | 2× | 256 | 128.441 us | 18.369 GB/s | 9.184 GB/s | 1.993 M |

`selected_tokens_per_page=1` 理论上会产生 4096 条 trace IO；当前运行中首次
触达顺序里恰好出现一组连续软件 page，被 `merge=true` 合并，所以实际为
4095 条。总读取字节没有变化。

随着每页命中 token 数增加：

- 读取放大从 `32×` 降到 `2×`；
- 有效需求带宽从 `1.76 GB/s` 提升到 `9.18 GB/s`；
- 但 trace 数和总介质字节下降，短 batch 更难填满 192 个执行资源，因此
  native/media bandwidth 反而下降。

这说明评估 KV page loading 时不能只看 media bandwidth。例如命中一个
token 就加载完整 32-token page 时，介质带宽达到 `56.3 GB/s`，但有效 KV
需求带宽只有 `1.76 GB/s`。

连续 page 控制组的64条相邻软件页会跨 page boundary 合并，最终同样生成
72条 `16 KiB` trace IO，结果和连续 token 控制组相同：

```text
时间 = 21.054 us
带宽 = 56.030 GB/s
```

## 7. 128 batch 并发实验

“128 batch 并发”表示生成128个独立 KV context，把所有请求拼接后通过
**一次** `MemoryEngine.issue_request()` 提交。它不是：

- 设置 `DP=128`；
- 设置 `storage_instance_num=128`；
- 分128次顺序调用 `issue_request()`；
- 创建128个 NVMe IO flow。

当前 trace 仍只有一个 NVMe flow，但所有请求的 arrival time 都是0。并发
主要由 QD=4096、controller on-the-fly window=1024 和 192 个阵列执行资源
表达。

总 workload 为：

```text
batch_count             = 128
tokens_per_batch        = 2048
logical token requests  = 128 × 2048 = 262,144
demand bytes            = 262,144 × 576
                        = 150,994,944 B
                        = 144 MiB
```

### 7.1 结果

| 场景 | Trace req | 平均trace IO | 时间 | Native BW | 有效需求 BW | IOPS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 batch contiguous | 72 | 16 KiB | 21.054 us | 56.030 GB/s | 56.030 GB/s | 3.420 M |
| 128 batch contiguous，同地址相位 | 9,216 | 16 KiB | 1,981.892 us | 76.187 GB/s | 76.187 GB/s | 4.650 M |
| 128 batch contiguous，16 KiB padding | 9,216 | 16 KiB | 1,662.276 us | 90.836 GB/s | 90.836 GB/s | 5.544 M |
| 128 batch contiguous，144 KiB padding | 9,216 | 16 KiB | 772.086 us | **195.568 GB/s** | **195.568 GB/s** | **11.936 M** |
| 128 batch sparse-uniform | 262,142 | 约 1 KiB | 24,031.388 us | 11.170 GB/s | 6.283 GB/s | 10.908 M |

`144 KiB` 地址布局达到饱和基准的：

```text
IOPS ratio = 11.936 / 12.362 = 96.6%
BW ratio   = 195.568 / 202.535 = 96.6%
```

9,216 条16 KiB请求虽然少于饱和基准的32,768条，但相当于192个执行资源上
约48轮工作，已经足够进入接近稳态的区间。

### 7.2 为什么增加 batch 后仍可能只有 76 GB/s

一个完整 context 的地址跨度为：

```text
131,072 tokens × 576 B
= 75,497,472 B
= 4,608 × 16 KiB
= 24 × 192 × 16 KiB
```

当前 CWDP 代理有192个执行资源，而 context stride 恰好是192个16 KiB页的
整数倍。因此每个 batch 都从各自 context 的 token 0 开始时：

```text
context_start_page mod 192 = 0
```

每个 batch 加载的数据为：

```text
2048 × 576 B = 1,179,648 B = 72 × 16 KiB
```

所以128个 batch 反复命中相同的72个资源相位，而不是均匀利用全部192路：

```text
资源利用比例        = 72 / 192 = 37.5%
预测带宽            = 202.535 × 37.5%
                    ≈ 75.95 GB/s
实测带宽            = 76.19 GB/s
```

实测与这个地址冲突模型基本一致。

### 7.3 地址着色实验

本文中的“地址着色”不是 MQSim 或正式 KV workload 已有的功能，而是实验
driver 手动改变各 context base address 的布局：

```python
base_addr = engine.get_tensor_addr(
    context_region_size + context_padding_bytes
)
```

Workload 仍只使用从 `base_addr` 开始的 context region，padding 作为两个
context 之间的空洞。它不改变每个 batch 选择的逻辑 token。

使用：

```text
context_padding_bytes = 144 KiB = 9 × 16 KiB
```

后，不同 context 的起始相位按9页错开，请求分布覆盖更多 channel/chip
proxy，结果达到 `195.568 GB/s`。把每个 batch 的 `start_token` 错开
256 tokens 也会移动 `9 × 16 KiB`，并得到相同结果，但会改变逻辑 token
选择，因此不如改变物理 base layout 清晰。

`144 KiB` 只是当前 `192-way proxy + 2048 tokens/batch` 的有效实验值，
不应写成所有 HBF 配置的默认参数。通用目标是让：

```text
context_start_16KiB_page mod 192
```

在并发 batch 间尽量均衡。

### 7.4 多 batch 稀疏 token

128个 sparse-uniform batch 提供了足够请求深度：

```text
logical requests       = 262,144
trace requests         = 262,142
native IOPS            = 10.908 M
```

少两条 trace 是因为聚合后的请求列表中偶然出现了可合并的相邻访问。其余
请求仍接近每条 `576 B → 1024 B`：

```text
native bytes           = 268,434,432 B
backend byte amp       ≈ 1.7778×
平均 trace IO          ≈ 1024 B
```

增加 batch 使 IOPS 从单 batch 平均约 `6.40 M` 上升到 `10.91 M`，接近设备
IOPS上限；但平均IO仍约1 KiB，所以：

```text
native bandwidth       = 11.17 GB/s
effective KV bandwidth = 6.28 GB/s
```

这说明增加 DSA/batch 并发可以打高 IOPS，但不能单独把小型随机 token
访问变成200 GB/s。若希望稀疏KV访问也获得高带宽，需要在DSA或上层布局中
做额外聚合，例如：

- 按16 KiB范围对token gather请求排序和合并；
- 读取完整KV page或superpage；
- 让不同batch的page基址跨192个proxy资源均衡分布；
- 同一batch的连续请求使用batch-major顺序提交，避免跨batch token
  交错破坏 `merge_contiguous`。

这些措施可能引入额外读取放大，需要同时报告native bandwidth和有效需求
bandwidth。

## 8. 主要结论

1. 当前 HBF 配置不是直接使用 `32 planes/die`，而是用
   `6 channels × 32 one-die chips/channel` 代理 `6 dies × 32 independent
   planes/die`。
2. `Plane_No_Per_Die` 必须保持1；这里的192个MQSim die是plane execution
   proxy，不是192个真实HBF die。
3. 当前配置的理论阵列上界为12.8 M IOPS / 209.7 GB/s，native饱和结果为
   12.36 M IOPS / 202.5 GB/s。
4. `merge_contiguous=true` 对连续token非常重要：单batch的2048个连续token
   可变成72条16 KiB IO；随机token通常仍是约1 KiB IO。
5. 增加到128 batch 能提供足够请求深度，但地址分布不均时仍只能达到约
   76 GB/s。
6. 在当前代理中对context base做地址着色后，128 batch连续加载达到
   11.94 M IOPS / 195.57 GB/s，接近饱和结果。
7. 128 batch随机稀疏token达到10.91 M IOPS，但native带宽只有11.17 GB/s，
   有效KV带宽只有6.28 GB/s。瓶颈从“并发不足”变成“小IO粒度和sector放大”。
8. Page loading能减少IO数量，但低页内命中率会产生2–32倍workload读取
   放大；必须用有效需求带宽判断KV加载收益。

## 9. 当前模型没有覆盖的成本

当前结果不应直接解释为真实系统端到端性能，模型尚未覆盖：

- 真实 HBF PHY、subplane 和 4.8 GT/s 接口；
- DSA descriptor生成、gather/coalescing硬件成本；
- CPU、IOMMU、TLB、page table和软件调度；
- 多个真实NVMe queue或多个独立DSA engine；
- 有限外部arrival rate和batch到达间隔；
- mapping-table miss、预条件化、GC/WL和读写混合干扰；
- 真实容量、写入、寿命、功耗和热约束；
- 请求级p99/p999及跨plane条带尾延迟。

当前配置适合回答：

> 在15 us SLC array latency、192路独立plane代理、16 KiB有效读取单元和深
> outstanding window下，地址分布、请求合并和KV读取放大会怎样影响可达到
> 的IOPS与带宽？

如果后续需要更接近HBF介质子系统而不是NVMe SSD代理，应优先把Python
wrapper切换到MQSim `DIRECT` path，再单独建模DSA到HBF的传输和请求接纳。

## 10. 验证

HBF几何、workload资源选择和 `merge_contiguous=true` 有对应测试：

```bash
python -m pytest tests/test_mqsim_media.py -q
```

本文生成时结果：

```text
25 passed
```

饱和16 KiB基准可以从仓库父目录运行：

```bash
python -m storage_mem_sim.run \
  --config storage_mem_sim/configs/mqsim_hbf_slc_6die_32plane_proxy.json \
  --num-requests 32768 \
  --size 16384
```

单 batch KV 实验可以使用 `docs/kv_cache_load_workload.md` 中的
programmatic API，把本节统一参数写入 `KVCacheLoadConfig`。128 batch
实验当前需要在driver中分别生成128个 workload，将地址、大小和类型列表
拼接后进行一次 `issue_request()`；正式 workload 尚未提供
`batch_count`、`context_padding_bytes` 或 `address_coloring` 配置字段。
