# Trace 生成分析

> `media/mqsim_wrapper/pymqsim/trace.py` — `build_trace_lines()` 的 trace 生成逻辑。
> 以示例说明各种 addr / size / merge 组合下的 trace 输出。

---

## 一、基本概念

| 常量 | 值 | 说明 |
|------|-----|------|
| `SECTOR_SIZE` | 512 B | MQSim 硬编码，LBA 的最小单位 |
| `PAGE_SIZE_BYTES` | 8 KB（默认）/ 16 KB | NAND Page 大小，由 ssdconfig.xml 决定 |
| `SECTORS_PER_PAGE` | PAGE_SIZE / 512 | 每 page 包含的 sector 数 |
| `request_size` | 用户配置（如 4KB / 8KB / 128KB） | 单条 trace line 的最大字节数 |

**两条代码路径：**

- **Path A**（page-first traversal）：`merge_contiguous=False` 且 `request_size < PAGE_SIZE` 且所有 chunk 都是单 line 时触发，生成合成地址以优化 CWDP 通道分布
- **Path B**（normal slicing）：其余情况，按原始地址切片，对齐到 sector 边界

---

## 二、示例（默认配置：PAGE_SIZE=8192, SECTOR_SIZE=512）

以下示例默认 `request_size=8192`，除非特别说明。

### 2.1 对齐地址 + merge=True

```
输入: addr=0, size=4096   (4KB, sector 对齐)
→ 1 line:  addr=0, size=4096  →  lba=0, sectors=8
```

```
输入: addr=0,size=4096 + addr=4096,size=4096  (连续)
→ merge: addr=0, size=8192
→ 1 line:  addr=0, size=8192  →  lba=0, sectors=16
```

```
输入: addr=0,size=4096 + addr=16384,size=4096  (有 gap)
→ 不合并
→ 2 lines: [addr=0,size=4096], [addr=16384,size=4096]
```

```
输入: addr=0,size=32768  (32KB > request_size)
→ 切片: 4 lines × 8KB
→ addrs=[0, 8192, 16384, 24576], sizes=[8192]×4
```

### 2.2 非对齐地址（BUG-1 已修复）

```
输入: addr=100, size=4096
→ sector 对齐: [0, 4608) = 4608 B
→ 1 line:  addr=0, size=4608  →  lba=0, sectors=9
```

```
输入: addr=100, size=16384, request_size=4096
→ sector 对齐: [0, 16896) = 16896 B
→ 5 lines:  addrs=[0,4096,8192,12288,16384]
             sizes=[4096,4096,4096,4096,512]
```

```
输入: addr=100, size=20480  (跨 page 边界 8192), request_size=32768
→ sector 对齐: [0, 20992) = 20992 B
→ 1 line:  addr=0, size=20992
```

### 2.3 对齐地址 + merge=False（无合并）

**Path A 触发条件：** `merge=False` 且 `request_size < PAGE_SIZE` 且每 chunk 单 line。

```
条件: PAGE_SIZE=8192, request_size=4096
输入: 4 个分散的 4KB 请求 (addr=0,100000,200000,300000)
→ Path A:
  lines_per_page = 8192/4096 = 2
  total_pages = ceil(4/2) = 2
  遍历: off=0:pg=0→0, pg=1→8192
        off=1:pg=0→4096, pg=1→12288
→ addrs=[0, 8192, 4096, 12288], sizes=[4096]×4
  ⚠ 原始地址被合成地址替换（BUG-2）
```

**Path A 不触发的情况：**

```
条件: PAGE_SIZE=8192, request_size=8192  (request_size == PAGE_SIZE)
输入: 2 个分散的 8KB 请求 (addr=0, 100000)
→ req_size < PAGE_SIZE 不成立 → Path B
→ chunk 0: [0, 8192) → 1 line
  chunk 1: addr=100000 非对齐 → [99840, 108544) → 2 lines
→ 3 lines total
```

```
条件: PAGE_SIZE=8192, request_size=8192
输入: 1 个 12KB 请求 (> request_size)
→ single_line_chunks 不成立 → Path B
→ [0, 12800) → 2 lines: [0,8192], [8192,4608]
```

### 2.4 合并后跨 page

```
输入: 3 个连续 8KB 请求
→ merge: addr=0, size=24576
→ 切片: 3 lines × 8KB
→ addrs=[0, 8192, 16384]
  (8192=page边界, 16384=page边界，均为正确的位置)
```

```
输入: 4 个连续 4KB 请求
→ merge: addr=0, size=16384  (恰好 2 个 page)
→ request_size=16384 → 1 line
→ addr=0, size=16384
```

### 2.5 读写混合

```
输入: [R@0,4096], [W@4096,4096]
→ merge_sequential: 保持输入顺序
→ 不合并（类型不同）
→ 2 lines: type=[1,0]
```

---
