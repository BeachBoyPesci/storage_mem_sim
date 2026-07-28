## MQSim 后端说明

本文同时记录两层内容：

1. 原生 MQSim C++ 仿真器如何从配置和 workload 构造 SSD/host 系统并执行离散事件仿真。
2. 本仓库 `MQSimMediaSystem`/`pymqsim` wrapper 如何把 `MemoryEngine` 请求转换为 MQSim trace 并取回指标。

`media/mqsim_wrapper/MQSim/` 是外部 MQSim 子模块；以下原生 MQSim 代码路径用于解释模型语义，除非任务明确要求，不应把它当作本项目日常改动区域。

### 原生 MQSim 整体框架

MQSim 是离散事件 SSD 仿真器，而不是 cycle-level 全系统模拟器。所有主要模块都是 `Sim_Object`，通过全局 `Simulator` 注册未来事件；事件循环每次取最早事件，更新仿真时间，并调用目标对象的 `Execute_simulator_event()`。因此，MQSim 的时间来自各模块显式建模的时延：trace 到达、PCIe TLP 传输、NVMe SQE/data DMA、cache DRAM access、NAND command/address/data transfer、die read/program/erase execution、GC 搬移和擦除等。

原生入口是：

```bash
MQSim -i ssdconfig.xml -w workload.xml
```

代码流程位于 `media/mqsim_wrapper/MQSim/src/main.cpp`：

```text
command_line_args()
  -> read_configuration_parameters(ssdconfig.xml)
  -> read_workload_definitions(workload.xml)
  -> for each IO_Scenario:
       Simulator->Reset()
       SSD_Device(...)
       Host_System(...)
       host.Attach_ssd_device(&ssd)
       Simulator->Start_simulation()
       collect_results(...)
```

也就是说，一个 `ssdconfig.xml` 描述硬件和 SSD 内部策略，一个 `workload.xml` 可以包含多个 `IO_Scenario`；每个 scenario 单独 reset 仿真器、重新构造一套 `SSD_Device + Host_System` 并输出结果。这种组织方式适合在同一设备配置上批量扫不同 workload/flow/QoS 场景。

从模块链路看，原生 MQSim 的层级比“host -> PCIe/NVMe -> FTL -> NAND”更完整：

```text
trace / synthetic workload
  -> IO_Flow / host-side SQ/CQ state
  -> PCIe Root Complex + PCIe Link + PCIe Switch
  -> NVMe Host Interface
       doorbell -> fetch SQE -> write data DMA -> read data DMA -> CQE
  -> Data Cache Manager
  -> FTL firmware
       Address Mapping Unit / CMT
       Flash Block Manager
       GC & Wear Leveling
       Transaction Scheduling Unit
  -> ONFI/NVDDR2 PHY + channels
  -> flash chip / die / plane / block / page
```

### 原生 MQSim 输入文件

#### SSD/host 配置：`Execution_Parameter_Set`

`ssdconfig.xml` 的根节点是 `Execution_Parameter_Set`，包含两组参数：

```xml
<Execution_Parameter_Set>
  <Host_Parameter_Set>...</Host_Parameter_Set>
  <Device_Parameter_Set>...</Device_Parameter_Set>
</Execution_Parameter_Set>
```

`Host_Parameter_Set` 描述主机侧链路和统计开关：

| 参数 | 单位/取值 | 代表的场景或规格 |
|---|---:|---|
| `PCIe_Lane_Bandwidth` | GB/s per lane | MQSim 用于估算 PCIe TLP 传输时间。它是简化带宽参数，不是完整 PCIe generation/encoding/credit 模型。 |
| `PCIe_Lane_Count` | lane 数 | 常见为 x4；决定 host-device 链路总带宽上限。 |
| `SATA_Processing_Delay` | ns | SATA 模式下的整体软件/硬件处理延迟；NVMe 路径主要走 PCIe/NVMe 模型。 |
| `Enable_ResponseTime_Logging` | bool | 是否周期性记录 response time。 |
| `ResponseTime_Logging_Period_Length` | ns | response time 日志周期。 |

`Device_Parameter_Set` 描述 SSD 设备、FTL、cache、GC、NAND 组织和时序：

| 类别 | 关键参数 | 含义 |
|---|---|---|
| Host interface | `HostInterface_Type`, `IO_Queue_Depth`, `Queue_Fetch_Size` | NVMe/SATA；NVMe 下 `IO_Queue_Depth` 是 submission/completion queue 深度，`Queue_Fetch_Size` 是设备侧一次可抓取/在途处理的 SQE 数量上限。 |
| Data cache | `Caching_Mechanism`, `Data_Cache_Sharing_Mode`, `Data_Cache_Capacity`, `Data_Cache_DRAM_*` | SSD 内部 data cache/DRAM。影响 read hit、write cache、destage、backpressure 和 cache DRAM access time。 |
| Mapping | `Address_Mapping`, `Ideal_Mapping_Table`, `CMT_Capacity`, `CMT_Sharing_Mode` | FTL 映射粒度和 mapping cache。`Ideal_Mapping_Table=false` 时，CMT miss 会产生额外 mapping read/write 到 NAND。 |
| Placement | `Plane_Allocation_Scheme` | LPA 到 channel/chip/die/plane 的分布策略，如 `CWDP`。会影响并行度、channel balance 和 multiplane 机会。 |
| Scheduling | `Transaction_Scheduling_Policy` | flash transaction 调度策略，如 `OUT_OF_ORDER`、`PRIORITY_OUT_OF_ORDER`、`FLIN`。影响 QoS、读写/GC/mapping 请求的仲裁。 |
| GC/WL | `Overprovisioning_Ratio`, `GC_Exec_Threshold`, `GC_Block_Selection_Policy`, `Use_Copyback_for_GC`, `Preemptible_GC_Enabled`, `GC_Hard_Threshold`, wear leveling 参数 | 预留空间、GC 触发阈值、victim block 策略、GC 是否 copyback、GC 是否可抢占、动态/静态 wear leveling。 |
| NAND topology | `Flash_Channel_Count`, `Chip_No_Per_Channel`, `Die_No_Per_Chip`, `Plane_No_Per_Die` | 后端并行度。channel 决定总线并行，chip/die/plane 决定 interleaving 和 multiplane 机会。 |
| NAND channel | `Flash_Channel_Width`, `Channel_Transfer_Rate`, `Flash_Comm_Protocol` | NAND channel 宽度和 MT/s。原生 MQSim 只实现 `NVDDR2` 协议模型。 |
| NAND timing | `Page_Read_Latency_LSB/CSB/MSB`, `Page_Program_Latency_LSB/CSB/MSB`, `Block_Erase_Latency`, suspend latency | NAND cell read/program/erase 时延，单位 ns。SLC/MLC/TLC 会选择不同 page latency 类型。 |
| NAND geometry | `Page_Capacity`, `Page_Metadat_Capacity`, `Page_No_Per_Block`, `Block_No_Per_Plane` | flash page/block/plane 容量组织。`Page_Capacity` 是数据区字节数，MQSim trace 的 sector 会按 page 拆成 flash transaction。 |

原生默认 `media/mqsim_wrapper/MQSim/ssdconfig.xml` 大致代表 FAST'18 时代的研究型 NVMe SSD：PCIe x4、每 lane 1 GB/s；8 channels、4 chips/channel、2 dies/chip、2 planes/die；8 KiB page；NAND channel 333 MT/s；read 75 us、program 750 us、erase 3.8 ms；page-level mapping、2 MiB CMT、7% overprovisioning、RGA GC。它适合研究架构瓶颈和相对趋势，但不应直接当作当前高端 PCIe 5/6 SSD 或新一代 3D QLC NAND 的规格。

#### Workload 配置：`MQSim_IO_Scenarios`

`workload.xml` 的根节点是 `MQSim_IO_Scenarios`：

```xml
<MQSim_IO_Scenarios>
  <IO_Scenario>
    <IO_Flow_Parameter_Set_Synthetic>...</IO_Flow_Parameter_Set_Synthetic>
    <IO_Flow_Parameter_Set_Trace_Based>...</IO_Flow_Parameter_Set_Trace_Based>
  </IO_Scenario>
</MQSim_IO_Scenarios>
```

一个 `IO_Scenario` 表示一次完整实验；一个 scenario 内可以包含多个 flow，用于模拟多 stream、多 queue、不同优先级或资源分区的并发 workload。

所有 flow 的公共字段：

| 参数 | 含义 |
|---|---|
| `Priority_Class` | NVMe 路径下的 flow 优先级，可用于 `PRIORITY_OUT_OF_ORDER` TSU 调度。 |
| `Device_Level_Data_Caching_Mode` | 该 flow 的 device cache 模式：关闭、write cache、read cache、read/write cache。 |
| `Channel_IDs`, `Chip_IDs`, `Die_IDs`, `Plane_IDs` | 分配给该 flow 的 NAND 资源集合。用于模拟资源隔离、共享、QoS 干扰。 |
| `Initial_Occupancy_Percentage` | preconditioning 时预先写入的逻辑空间比例，影响后续 GC 状态。 |

Synthetic flow 由 MQSim 内部生成请求，适合扫参数和找瓶颈：

| 参数 | 含义 |
|---|---|
| `Synthetic_Generator_Type=BANDWIDTH` | 按目标带宽/到达间隔生成请求，适合研究 offered load。 |
| `Synthetic_Generator_Type=QUEUE_DEPTH` | 尝试维持目标平均队列深度，适合研究 QD 对 IOPS/latency 的影响。 |
| `Read_Percentage` | 读写比例。 |
| `Address_Distribution` | `STREAMING`、`RANDOM_UNIFORM`、`RANDOM_HOTCOLD`。 |
| `Working_Set_Percentage` | 工作集占该 flow 可见 LBA 范围的比例。 |
| `Request_Size_Distribution` | `FIXED` 或 `NORMAL`。 |
| `Average_Request_Size`, `Variance_Request_Size` | 请求大小，单位是 sector，不是 byte。 |
| `Average_No_of_Reqs_in_Queue` | queue-depth 生成器的目标平均队列长度。 |
| `Bandwidth` | bandwidth 生成器的目标带宽，单位 bytes/s。 |
| `Stop_Time`, `Total_Requests_To_Generate` | 停止条件。 |

Trace-based flow 重放外部 trace，适合对真实或上层生成 workload 做 SSD 后端解释：

| 参数 | 含义 |
|---|---|
| `File_Path` | trace 文件路径。 |
| `Percentage_To_Be_Executed` | 执行 trace 的百分比。 |
| `Relay_Count` | trace 重放次数。 |
| `Time_Unit` | trace timestamp 单位：ps/ns/us。 |

原生 trace 行格式是：

```text
time device address size type
```

其中 `address` 和 `size` 是 sector/LBA 口径，`type=0` 表示 write，`type=1` 表示 read。trace 的到达时间会成为 host request 的 `Arrival_time`；请求真正进入 NVMe SQ 时记录 `Enqueue_time`。MQSim 输出中因此能区分端到端延迟和设备服务延迟。

### 本仓库 wrapper 架构

`MQSimMediaSystem` 通过 `pymqsim` Python 库对接 MQSim C++ 仿真器：

```
MemoryEngine.issue_request()
  → MQSimMediaSystem.handler_mem_request(mem_req_list)
    ├─ 1. write_trace_file()        → MQSim trace 文件
    ├─ 2. generate_workload_xml()   → workload XML
    ├─ 3. run_simulation()          → 调用 MQSim（native pybind11）
    └─ 4. 返回 MediaMetrics         → time, bandwidth, IOPS
```

其中 `IOPS`、`IOPS_Read` 和 `IOPS_Write` 直接来自 MQSim 的
`Host.IO_Flow` 输出，表示 trace 请求经过 NVMe queue、PCIe 和 SSD 内部路径后
得到的端到端 device IOPS。它们不是 MemoryEngine logical request rate，也不是
NAND 内部 page read/program transaction rate。仿真结束时 wrapper 会校验
MQSim 的 generated request 数与 serviced request 数一致，避免把未完成请求
计入 IOPS。

### MQSim trace 格式

每行一条请求：`<arrival_ns> <device_id> <lba> <sectors> <req_type>`

| 字段 | 说明 |
|---|---|
| `arrival_ns` | 到达时间（固定为 0，MemoryEngine 无时序） |
| `device_id` | 按 trace 行号轮转的设备/流标识：`line_index % CHANNELS`；它不是物理 NAND channel 映射 |
| `lba` | 逻辑块地址 = `addr / 512`（地址已 sector 对齐到 512B 边界，保留页内扇区偏移） |
| `sectors` | 扇区数 = `ceil(size / 512)` |
| `req_type` | 1 = 读, 0 = 写 |

> NAND 的 channel/chip/die/plane 分配由 MQSim 的 FTL 和
> `Plane_Allocation_Scheme` 决定，trace 生成器不自行重写 LBA 来模拟 CWDP。

### 请求合并

`merge_sequential()` 自动合并同一请求类型、地址连续的 MemoryRequest：

- **触发条件**: `addr[i] + size[i] == addr[i+1]` 且 `req_type[i] == req_type[i+1]`
- **带宽测试** (`merge_contiguous: true`): 开启合并 → 少量大 I/O → 饱和通道带宽
- **IOPS 测试** (`merge_contiguous: false`): 关闭合并 → 大量小 I/O → 测量每操作延迟

### 控制参数 (`mqsim.json`)

| 参数 | 带宽测试 | IOPS 测试 | 说明 |
|------|---------|----------|------|
| `merge_contiguous` | `true` | `false` | 是否合并连续同类请求 |
| `request_size` | 131072 (128 KB) | 4096 (4 KB) | 每条 trace line 最大字节数 |

> `merge_contiguous` 和 `request_size` 通过 `MediaConfig` →
> `MQSimMediaSystem._init_mqsim()` → `TraceSliceConfig` 自动传递。

### MQSim 配置文件

- **SSD 设备配置** (`default_ssdconfig.xml`): 定义通道数、芯片数、NAND 参数、FTL 策略等。NAND 几何参数在 `MQSimMediaSystem` 初始化时自动解析并加载到 `trace` 模块
- **Workload 配置** (`default_workload.xml`): 定义 I/O 场景、trace 文件路径占位符、时间单位等

指定自定义配置：

```json
{
    "ssd_config": "path/to/custom_ssdconfig.xml",
    "workload_config": "path/to/custom_workload.xml"
}
```

### pymqsim 库独立使用

```python
from pymqsim import (
    TraceSliceConfig, write_trace_file, load_from_ssdconfig_xml,
    generate_workload_xml, run_simulation,
    # 理论公式（无需运行仿真即可预估性能）
    theory_iops, theory_bandwidth_mbps, theory_bus_utilization,
)

# 1. 加载几何并生成 trace 文件
load_from_ssdconfig_xml("ssdconfig.xml")
cfg = TraceSliceConfig(merge_contiguous=True, request_size=131072)
total_bytes, lines = write_trace_file(mem_req_list, "trace.txt", cfg)

# 2. 生成 workload XML（基于 default_workload.xml 模板，替换 trace 路径）
generate_workload_xml("trace.txt", "workload.xml")

# 3. 运行仿真
result = run_simulation(
    ssd_config_path="ssdconfig.xml",
    workload_xml_path="workload.xml",
)

print(f"Bandwidth: {result.bandwidth_bytes_per_sec / 1e9:.2f} GB/s")
print(f"IOPS:     {result.total_iops:.0f}")

# 4. 理论预估（不运行仿真）
for size in [4096, 8192, 32768, 65536, 131072]:
    iops = theory_iops(size)
    bw = theory_bandwidth_mbps(size)
    util = theory_bus_utilization(size)
    print(f"{size//1024}KB → IOPS={iops:,.0f}  BW={bw:,.0f} MB/s  U={util:.1%}")
```

输出示例：

```
4KB   → IOPS=633,899  BW=2,596 MB/s  U=97.5%   (带宽-Bound)
8KB   → IOPS=321,020  BW=2,630 MB/s  U=98.7%   (带宽-Bound)
32KB  → IOPS=81,035   BW=2,655 MB/s  U=99.7%   (强带宽-Bound)
64KB  → IOPS=40,583   BW=2,660 MB/s  U=99.8%   (强带宽-Bound)
128KB → IOPS=20,308   BW=2,662 MB/s  U=99.9%   (强带宽-Bound)
```

### 项目结构

```
configs/mqsim/
├── default_ssdconfig.xml      # 默认 SSD 设备配置（NAND 几何参数来源）
├── default_workload.xml       # 默认 workload 模板
├── ascend_a3_16ch_*.xml       # A3/ES3600P V7 3.2TB 校准代理模型
└── pm1753_*.xml               # PM1753 代理模型与 workload 配置

media/mqsim_wrapper/
├── pymqsim/                   # Python 库
│   ├── __init__.py            # 公开 API（27 个符号）
│   ├── trace.py               # 几何常量 + CWDP + trace 生成
│   ├── performance_model.py   # 理论 IOPS、带宽和总线利用率
│   ├── workload.py            # generate_workload_xml — workload XML 生成
│   ├── simulator.py           # run_simulation — 仿真运行器（native + subprocess）
│   └── output.py              # MQSimResult — 输出 XML 解析
├── MQSim/                     # MQSim C++ 子模块
├── mqsim_pybind.cpp           # pybind11 C++ 桥接
├── CMakeLists.txt             # C++ 构建脚本
├── setup.py                   # pip install 入口
└── trace/                     # 运行时生成的 trace 和 workload 文件
```

### MQSim 构建

MQSim 提供两种产物：

| 产物 | 说明 | 用途 |
|---|---|---|
| `_mqsim.so` (pybind11) | Python 原生扩展，`run_simulation()` 直接调用 | 在 Python 代码中运行仿真 |
| `MQSim` (二进制) | 独立的命令行可执行文件 | `./MQSim -i ssdconfig.xml -w workload.xml` 手动运行 |

#### 环境要求

- **操作系统**：Linux 或 WSL（Windows 原生不支持）
- **编译器**：`g++`（支持 C++11）
- **Python**：3.10+
- **CMake**：3.14+（pybind11 扩展需要）

#### 方式一：只安装 pybind11 扩展（Python 库）

```bash
# 1. 初始化 MQSim 子模块
git submodule update --init media/mqsim_wrapper/MQSim

# 2. 编译并安装
cd media/mqsim_wrapper && pip install -e .

# 3. 验证
python -c "from pymqsim import check_mqsim_available; print(check_mqsim_available())"
```

#### 方式二：只编译 MQSim 二进制

```bash
# 1. 初始化 MQSim 子模块
git submodule update --init media/mqsim_wrapper/MQSim

# 2. 编译 MQSim 二进制
cd media/mqsim_wrapper/MQSim && make

# 3. 验证
./MQSim -i ssdconfig.xml -w workload.xml
```

产物 `MQSim` 在 `media/mqsim_wrapper/MQSim/` 目录下。

#### 方式三：同时安装两种

```bash
# 1. 初始化子模块
git submodule update --init media/mqsim_wrapper/MQSim

# 2. 编译 MQSim 二进制
cd media/mqsim_wrapper/MQSim && make
cd ../..

# 3. 编译 pybind11 扩展并安装 pymqsim
cd media/mqsim_wrapper && pip install -e .
cd ../..

# 4. 验证两者都可用
python -c "from pymqsim import check_mqsim_available; print('pybind11:', check_mqsim_available())"
test -x media/mqsim_wrapper/MQSim/MQSim && echo "binary: OK"
```

> **注意**：如果 `_mqsim` 模块未构建，`handler_mem_request` 会抛出 `RuntimeError` 并提示构建命令。
