## MQSim 后端说明

### 架构

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
