# MemEngine

MemEngine 是一个面向 LLM 推理服务场景的内存/存储介质仿真框架。它位于上层服务/模型逻辑和底层介质仿真器之间，将 tensor、KV cache 等高层访问转换成后端可执行的请求，并返回 timing、bandwidth，以及 MQSim 端到端 device IOPS 等性能指标。

更详细的设计说明见 [docs/design.md](docs/design.md)。

## 后端说明

| 后端 | 用途 | 当前状态 |
| --- | --- | --- |
| `ANALYTIC` | 快速 roofline 估算，`time = bytes / bandwidth` | 可直接使用 |
| `RAMULATOR` | 通过 Ramulator2 Python bindings 进行 DRAM 周期级仿真 | 安装/构建 Ramulator2 后可用 |
| `MQSIM` | SSD/NVMe 仿真，通过 pymqsim Python 库对接 MQSim C++ 仿真器 | trace 生成、CWDP 地址分配、XML 输出解析已完成 |

## 快速开始

```bash
# 1. 拉取代码
git clone <repo-url>
cd storage_mem_sim

# 2. 安装 Python 依赖
pip install pyyaml

# 3. 运行 Analytic 示例
cd ..
python -m storage_mem_sim.run -c storage_mem_sim/configs/analytic.json

# 可选：调整请求数量和单请求大小
python -m storage_mem_sim.run -c storage_mem_sim/configs/analytic.json --num-requests 32 --size 128

# 可选：运行随机稀疏 page KV cache workload
python -m storage_mem_sim.run \
  -c storage_mem_sim/configs/analytic.json \
  -w storage_mem_sim/configs/workloads/kv_sparse_page.json

# 4. 如需运行 Ramulator2 后端
cd storage_mem_sim
git submodule update --init media/ramulator_wrapper/ramulator2
pip install -e media/ramulator_wrapper

# 5. 运行 Ramulator2 示例
cd ..
python -m storage_mem_sim.run -c storage_mem_sim/configs/ramulator.json

# 6. 如需运行 MQSim 后端
cd storage_mem_sim
git submodule update --init media/mqsim_wrapper/MQSim
pip install -e media/mqsim_wrapper

# 7. 运行 MQSim 示例（带宽 bound：合并 + 大 I/O）
python -m storage_mem_sim.run -c storage_mem_sim/configs/mqsim.json --num-requests 64 --size 131072

# 8. MQSim IOPS bound（不合并 + 小 I/O）
python -m storage_mem_sim.run -c storage_mem_sim/configs/mqsim.json --num-requests 1024 --size 4096
```
