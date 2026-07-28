# MQSim XML 配置文件参数说明

---

## 一、`default_ssdconfig.xml` — SSD 设备配置

根元素 `<Execution_Parameter_Set>` 包含三个子集。

### 1.1 Host_Parameter_Set — 主机接口参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `PCIe_Lane_Bandwidth` | 1.0 | PCIe 单 lane 带宽 (GB/s)。Gen3=1.0, Gen4=2.0 |
| `PCIe_Lane_Count` | 4 | PCIe lane 数量。×4 常见于 NVMe SSD |
| `SATA_Processing_Delay` | 400000 | SATA 命令处理延迟 (ns)。仅 SATA 接口时使用，NVMe 下忽略 |
| `Enable_ResponseTime_Logging` | false | 是否启用响应时间日志（产生大量输出） |
| `ResponseTime_Logging_Period_Length` | 1000000 | 响应时间日志采样周期 (ns) |

### 1.2 Device_Parameter_Set — 设备级参数

#### 基础配置

| 参数 | 默认值 | 说明 |
|---|---|---|
| `Seed` | 321 | 随机数种子。控制 GC、wear leveling 等随机行为，相同种子可复现结果 |
| `Enabled_Preconditioning` | false | 是否启用预填充。true 时会先写满 SSD 到指定占用率再开始测试 |
| `Memory_Type` | FLASH | 存储介质类型。`FLASH` = NAND Flash |
| `HostInterface_Type` | NVME | 主机接口协议。`NVME` 或 `SATA` |

#### I/O 队列

| 参数 | 默认值 | 说明 |
|---|---|---|
| `IO_Queue_Depth` | 65535 | I/O 提交队列深度（最大排队 I/O 数） |
| `Queue_Fetch_Size` | 512 | 每次从队列取出的最大请求数 |

#### 数据缓存 (DRAM Cache)

| 参数 | 默认值 | 说明 |
|---|---|---|
| `Caching_Mechanism` | ADVANCED | 缓存策略。`ADVANCED` = 高级缓存管理 |
| `Data_Cache_Sharing_Mode` | SHARED | 缓存共享模式。`SHARED` = 多流共享 |
| `Data_Cache_Capacity` | 268435456 | 数据缓存总容量 (B)。256 MB |
| `Data_Cache_DRAM_Row_Size` | 8192 | DRAM 行大小 (B) |
| `Data_Cache_DRAM_Data_Rate` | 100 | DRAM 数据速率 (MT/s) |
| `Data_Cache_DRAM_Data_Busrt_Size` | 1 | DRAM burst 大小 |
| `Data_Cache_DRAM_tRCD` | 13 | DRAM RAS-to-CAS 延迟 (ns) |
| `Data_Cache_DRAM_tCL` | 13 | DRAM CAS 延迟 (ns) |
| `Data_Cache_DRAM_tRP` | 13 | DRAM 预充电时间 (ns) |

#### FTL 地址映射

| 参数 | 默认值 | 说明 |
|---|---|---|
| `Address_Mapping` | PAGE_LEVEL | 地址映射粒度。`PAGE_LEVEL` = 页级映射（最灵活，映射表最大） |
| `Ideal_Mapping_Table` | false | 是否假设无限映射表空间（无映射表开销） |
| `CMT_Capacity` | 2097152 | Cached Mapping Table 容量 (B)。2 MB，缓存热映射条目 |
| `CMT_Sharing_Mode` | SHARED | CMT 共享模式 |
| `Plane_Allocation_Scheme` | CWDP | Plane 分配策略。`CWDP` = Channel-Way-Die-Plane 交错写入 |
| `Transaction_Scheduling_Policy` | PRIORITY_OUT_OF_ORDER | 事务调度策略。乱序 + 优先级调度 |

#### 垃圾回收 (Garbage Collection)

| 参数 | 默认值 | 说明 |
|---|---|---|
| `Overprovisioning_Ratio` | 0.07 | 超配比。7% 额外空间用于 GC 写放大缓冲 |
| `GC_Exec_Threshold` | 0.05 | GC 触发阈值。空闲块比例低于 5% 时启动 GC |
| `GC_Block_Selection_Policy` | RGA | 回收块选择策略。`RGA` = Random Greedy Algorithm |
| `Use_Copyback_for_GC` | false | 是否使用 copyback 命令做 GC（减少总线占用） |
| `Preemptible_GC_Enabled` | false | GC 是否可被主机 I/O 抢占 |
| `GC_Hard_Threshold` | 0.005 | GC 硬阈值。空闲块低于 0.5% 时暂停主机 I/O 强制 GC |

#### Wear Leveling (磨损均衡)

| 参数 | 默认值 | 说明 |
|---|---|---|
| `Dynamic_Wearleveling_Enabled` | true | 动态磨损均衡。写入时优先选擦除次数少的空闲块 |
| `Static_Wearleveling_Enabled` | true | 静态磨损均衡。定期将冷数据迁移到擦除次数多的块 |
| `Static_Wearleveling_Threshold` | 100 | 静态磨损均衡触发阈值（最大最小擦除次数差） |

#### 命令暂停 (CMD Suspension)

| 参数 | 默认值 | 说明 |
|---|---|---|
| `Preferred_suspend_erase_time_for_read` | 700000 | 读请求可等待擦除暂停的最大时间 (ns) |
| `Preferred_suspend_erase_time_for_write` | 700000 | 写请求可等待擦除暂停的最大时间 (ns) |
| `Preferred_suspend_write_time_for_read` | 100000 | 读请求可等待写暂停的最大时间 (ns) |

#### NAND 通道与拓扑

| 参数 | 默认值 | 说明 |
|---|---|---|
| `Flash_Channel_Count` | 8 | NAND 闪存通道数。→ `CHANNELS` |
| `Flash_Channel_Width` | 1 | 通道宽度（总线位宽因子） |
| `Channel_Transfer_Rate` | 333 | 单通道数据传输速率 (MB/s)。MQSim 的整数 ns 周期要求该值不超过 1000。→ `CHANNEL_BW_MBPS` |
| `Chip_No_Per_Channel` | 4 | 每通道芯片数。→ `CHIPS_PER_CH` |
| `Flash_Comm_Protocol` | NVDDR2 | 闪存通信协议。`NVDDR2` / `NVDDR3`，决定 CMD/DATA 时序 |

### 1.3 Flash_Parameter_Set — NAND 闪存物理参数

#### 闪存技术

| 参数 | 默认值 | 说明 |
|---|---|---|
| `Flash_Technology` | MLC | 闪存类型。`SLC`(1bit/cell), `MLC`(2bit), `TLC`(3bit)。影响读写延迟和寿命 |
| `CMD_Suspension_Support` | ERASE | 支持暂停的命令类型。`ERASE` = 可暂停擦除以优先服务读/写 |

#### 读取延迟

| 参数 | 默认值 | 说明 |
|---|---|---|
| `Page_Read_Latency_LSB` | 75000 | LSB 页读取延迟 (ns)。→ `NAND_tR_NS` |
| `Page_Read_Latency_CSB` | 75000 | CSB 页读取延迟 (ns)。MLC 的中心有效位页 |
| `Page_Read_Latency_MSB` | 75000 | MSB 页读取延迟 (ns)。MLC 的最高有效位页 |

> MLC 每个 cell 存 2 bit，分为 LSB/MSB 页（或 LSB/CSB/MSB 三页架构）。不同类型的页读取延迟可能不同（MSB 通常更慢），此处默认值相同。

#### 写入延迟

| 参数 | 默认值 | 说明 |
|---|---|---|
| `Page_Program_Latency_LSB` | 750000 | LSB 页编程延迟 (ns)。750 μs |
| `Page_Program_Latency_CSB` | 750000 | CSB 页编程延迟 (ns) |
| `Page_Program_Latency_MSB` | 750000 | MSB 页编程延迟 (ns) |

#### 擦除参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `Block_Erase_Latency` | 3800000 | 块擦除延迟 (ns)。3.8 ms |
| `Block_PE_Cycles_Limit` | 10000 | 块擦写周期上限。MLC 典型 10K 次 |

#### 命令暂停时间

| 参数 | 默认值 | 说明 |
|---|---|---|
| `Suspend_Erase_Time` | 700000 | 擦除暂停最大等待时间 (ns) |
| `Suspend_Program_Time` | 100000 | 编程暂停最大等待时间 (ns) |

#### NAND 几何参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `Die_No_Per_Chip` | 2 | 每芯片 Die 数。→ `DIES_PER_CHIP` |
| `Plane_No_Per_Die` | 2 | 每 Die Plane 数。→ `PLANES_PER_DIE` |
| `Block_No_Per_Plane` | 2048 | 每 Plane 块数 |
| `Page_No_Per_Block` | 256 | 每块页数。→ `PAGES_PER_BLOCK` |
| `Page_Capacity` | 8192 | 页容量 (B)。→ `PAGE_SIZE_BYTES` |
| `Page_Metadat_Capacity` | 448 | 页元数据容量 (B)。存放 ECC、映射信息等 |

#### 几何总容量计算

```
总 Plane 数   = 8 × 4 × 2 × 2 = 128
总块数        = 128 × 2048 = 262,144
总页数        = 262,144 × 256 = 67,108,864
总容量        = 67,108,864 × 8192 = 512 GB  (不含 OP)
可用容量      = 512 / (1 + 0.07) ≈ 478.5 GB
```

---

## 二、`default_workload.xml` — 工作负载配置

根元素 `<MQSim_IO_Scenarios>` 包含一个或多个 `<IO_Scenario>`。

### 2.1 IO_Flow_Parameter_Set_Trace_Based — 基于 Trace 的 I/O 流

| 参数 | 默认值 | 说明 |
|---|---|---|
| `Priority_Class` | URGENT | I/O 优先级。`URGENT` = 最高优，另有 `HIGH`/`NORMAL`/`LOW` |
| `Device_Level_Data_Caching_Mode` | READ_CACHE | 缓存模式。`READ_CACHE` = 读缓存，`WRITE_CACHE` = 写缓存，`TURNED_OFF` = 关闭 |
| `Channel_IDs` | 0,1,2,3,4,5,6,7 | 工作负载使用的通道 ID 列表（逗号分隔） |
| `Chip_IDs` | 0,1,2,3 | 工作负载使用的芯片 ID 列表 |
| `Die_IDs` | 0,1 | 工作负载使用的 Die ID 列表 |
| `Plane_IDs` | 0,1 | 工作负载使用的 Plane ID 列表 |
| `Initial_Occupancy_Percentage` | 50 | 初始空间占用率 (%)。50% = 半满状态 |
| `File_Path` | traces/tpcc-small.trace | Trace 文件路径。运行时被 `generate_workload_xml()` 替换为实际路径 |
| `Percentage_To_Be_Executed` | 100 | 执行 trace 的百分比 (1-100) |
| `Relay_Count` | 1 | 回放次数。>1 时循环回放 trace |
| `Time_Unit` | NANOSECOND | 时间单位。`NANOSECOND` = trace 中的时间戳按纳秒解释 |

### 2.2 资源 ID 与 SSD 几何的关系

`Channel_IDs` / `Chip_IDs` / `Die_IDs` / `Plane_IDs` 定义了工作负载使用的**子资源集**。它们必须与 `ssdconfig.xml` 中的硬件范围一致：

```
ssdconfig.xml                    workload.xml 范围
──────────────────────────────────────────────────
Flash_Channel_Count = 8    →    Channel_IDs = 0..7
Chip_No_Per_Channel = 4    →    Chip_IDs    = 0..3
Die_No_Per_Chip = 2        →    Die_IDs     = 0..1
Plane_No_Per_Die = 2       →    Plane_IDs   = 0..1
```

可以指定子集（如只用 4 个通道），但范围不能超出硬件定义。


设备id：
| 参数                        | 值          | 对多流的影响                                       |
| ------------------------- | ---------- | -------------------------------------------- |
| `IO_Queue_Depth`          | **65535**  | 每个 device\_id 对应的 SQ 最大深度（NVMe 上限）           |
| `Queue_Fetch_Size`        | **512**    | 每个 SQ 每次最多取 512 个请求进入设备级队列                   |
| `Data_Cache_Sharing_Mode` | **SHARED** | 所有 device\_id 的流**共享** 256MB Data Cache，互相竞争 |
| `CMT_Sharing_Mode`        | **SHARED** | 所有 device\_id 的流**共享** 2MB 映射缓存，互相竞争         |
| `PCIe_Lane_Count`         | **4**      | 前端带宽 = 4 GB/s（1GB/s × 4 lanes），是所有流共享的总线     |
增加 device_id 个数的核心好处是提升并发度、更充分利用 SSD 后端并行资源，从而更接近真实 NVMe 多队列场景下的性能上限。具体可以从以下几个维度理解：

## 三、如何根据配置参数计算理论iops和理论带宽

### 一、核心公式

`pymqsim.performance_model` 用多个独立资源上界的最小值作为理论 IOPS：

```text
TotalDies   = Channels × ChipsPerCH × DiesPerChip
TotalPlanes = TotalDies × PlanesPerDie
DiesPerCH   = ChipsPerCH × DiesPerChip
PagesPerIO  = ceil(RequestSize / PageSize)

DataOut     = RequestSize × 1000 / ChannelRateMBps
BusTime     = CmdTransfer + DataSetup + DataOut
PipeCycle   = max(tR, DiesPerCH × BusTime)

IOPS_channel = Channels × ChannelRateMBps × 1e6 / RequestSize
IOPS_nand    = TotalPlanes × 1e9 / (PagesPerIO × tR)
IOPS_cwdp    = TotalDies × 1e9 / PipeCycle
IOPS_pcie    = PCIeLaneGBps × LaneCount × 1e9 / RequestSize
IOPS_qd      = QueueDepth × 1e9 / (tR + BusTime)

TheoryIOPS = min(IOPS_channel, IOPS_nand, IOPS_cwdp, IOPS_pcie, IOPS_qd)
TheoryBW   = min(TheoryIOPS × RequestSize / 1e6,
                 Channels × ChannelRateMBps)
BusUtil    = DiesPerCH × DataOut / PipeCycle
```

这些公式是理想化上界，不包含 MQSim 的完整调度、FTL、GC、缓存和 host
开销。性能报告应同时给出请求大小、读写比例、地址模式、队列深度与所用
XML，不能只引用理论值。
