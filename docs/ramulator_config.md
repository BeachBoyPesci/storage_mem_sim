# Ramulator 配置说明

本文档总结当前仓库内 Ramulator2 Python wrapper 支持的 DRAM `org.preset`、`timing.preset`、可 override 参数，以及 HBM channel / pseudo-channel 的配置语义。

## 配置结构

Ramulator 后端使用 JSON 指向 YAML：

```json
{
    "mem_type": "HBM",
    "media_config": {
        "media_type": "ramulator",
        "capacity": 96.0,
        "config": "configs/ramulator/ascend_a5_950dt_hbm.yaml",
        "instances": 1
    }
}
```

YAML 中 DRAM 类型、组织和时序一般写在 `MemorySystem.DRAM` 下：

```yaml
MemorySystem:
  impl: GenericDRAM
  clock_ratio: 1
  ChannelMapper:
    impl: CacheLineInterleave
  DRAM:
    impl: HBM3
    org:
      preset: HBM3_32Gb_8hi
    timing:
      preset: HBM3_6400Mbps
      rate: 8000
      tCK_ps: 250
  Controllers:
    - impl: HBM34
      count: 64
      Scheduler:
        impl: FRFCFS
      RowPolicy:
        impl: Open
      AddrMapper:
        impl: RoBaRaCoCh
      RefreshManager:
        impl: NoRefresh
```

`Controllers[].count` 是本仓库 wrapper 增加的简写，不是 Ramulator2 原生字段。它等价于重复声明多个同构 controller。Ramulator2 原生语义是：

```text
num_channels = len(Controllers)
```

每个 controller 对应一个 physical channel。

## Override 规则

`org.preset` 会先展开为默认 org 参数，然后可 override 部分 org 字段。代码规则见 `ramulator/dram/spec.py`：

- 可以 override DRAM hierarchy level，例如 `rank`、`pseudochannel`、`sid`、`bankgroup`、`bank`、`row`、`column`
- 可以 override `channel_width`
- 不允许 override `channel`
- 当前不支持 override `dq` 和 `density`

`timing.preset` 会先展开为默认 timing 参数，然后可 override 该 DRAM 标准 `timing_params` 中存在的字段。常见字段包括 `rate`、`tCK_ps`、`nCL`、`nRCD*`、`nRP`、`nRAS`、`nRC` 等。

如果 override `rate`，建议同步 override `tCK_ps`：

```text
tCK_ps = 2e6 / rate
```

否则理论带宽和时序周期/真实时间之间会不一致。

## Channel 与 PseudoChannel

HBM 的层级包含 pseudo-channel：

```text
HBM2:       Channel -> PseudoChannel -> BankGroup -> Bank -> Row -> Column
HBM3/HBM4: Channel -> PseudoChannel -> Sid -> BankGroup -> Bank -> Row -> Column
```

含义：

- `Controllers[].count` 决定 physical channel 数。
- `org.pseudochannel` 决定每个 physical channel 内部有多少 pseudo-channel。
- 当前 HBM2/HBM3/HBM4 preset 中 `pseudochannel = 2`。

HBM 理论带宽公式应计入 pseudo-channel：

```text
BW = channel_count * pseudochannel * channel_width / 8 * rate
```

例如 HBM3/HBM3E，64 channels、2 pseudochannels/channel、32-bit pseudo-channel、8000 Mbps：

```text
64 * 2 * 32 / 8 * 8000e6 = 4096 GB/s
```

容量应按 DRAM hierarchy 计算：

```text
capacity_per_controller_bits =
  dq * product(level_counts excluding Channel)

total_capacity =
  controller_count * capacity_per_controller_bits / 8
```

注意：MemEngine JSON 里的 `capacity` 是对外地址空间容量；Ramulator YAML 的 org 是仿真拓扑容量。当前代码不会自动校验二者一致。

## ChannelMapper 限制

当前 Ramulator2 原生可用的 channel mapper 包括：

- `CacheLineInterleave`
- `PassThroughChannelMapper`

`CacheLineInterleave` 要求 channel 数是 2 的幂。如果 `Controllers[].count` 不是 2 的幂，例如 72 或 96，原生 mapper 会报错：

```text
CacheLineInterleave requires a power-of-two channel count
```

因此，在不修改 Ramulator2 源码的前提下，建议用于可执行仿真的 channel 数选择 1、2、4、8、16、32、64、128 等。

## 支持的 Preset 与 Override 字段

本节按 DRAM 标准汇总当前 Python wrapper 暴露的可配置项。表中的字段含义如下：

| 字段 | 配置位置 | 说明 |
| --- | --- | --- |
| Levels | `DRAM.org` 内部展开结果 | DRAM 地址层级。`Channel` 由 controller 数决定，不能在 `org` 中 override。 |
| Org override | `DRAM.org.<field>` | 可在 `org.preset` 基础上覆盖的组织参数，影响容量、地址映射层级规模和 transaction 粒度。 |
| Timing override | `DRAM.timing.<field>` | 可在 `timing.preset` 基础上覆盖的时序参数，影响理论速率和命令间隔。 |
| Org presets | `DRAM.org.preset` | 内置容量/组织 preset。 |
| Timing presets | `DRAM.timing.preset` | 内置速率/时序 preset。 |

### 配置项总览

| 标准 | Levels | Org override | Timing override |
| --- | --- | --- | --- |
| DDR3 | `Channel, Rank, Bank, Row, Column` | `rank`, `bank`, `row`, `column`, `channel_width` | `rate`, `nBL`, `nCL`, `nRCD`, `nRP`, `nRAS`, `nRC`, `nWR`, `nRTP`, `nCWL`, `nCCD`, `nRRD`, `nWTR`, `nFAW`, `nRFC`, `nREFI`, `nCS`, `tCK_ps` |
| DDR4 | `Channel, Rank, BankGroup, Bank, Row, Column` | `rank`, `bankgroup`, `bank`, `row`, `column`, `channel_width` | `rate`, `nBL`, `nCL`, `nRCD`, `nRP`, `nRAS`, `nRC`, `nWR`, `nRTP`, `nCWL`, `nCCDS`, `nCCDL`, `nRRDS`, `nRRDL`, `nWTRS`, `nWTRL`, `nFAW`, `nRFC`, `nREFI`, `nCS`, `tCK_ps` |
| DDR5 | `Channel, Rank, BankGroup, Bank, Row, Column` | `rank`, `bankgroup`, `bank`, `row`, `column`, `channel_width` | `rate`, `nBL`, `nCL`, `nRCD`, `nRP`, `nRAS`, `nRC`, `nWR`, `nRTP`, `nCWL`, `nPPD`, `nCCDS`, `nCCDL`, `nCCDS_WR`, `nCCDL_WR`, `nRRDS`, `nRRDL`, `nWTRS`, `nWTRL`, `nFAW`, `nRFC`, `nREFI`, `nCS`, `tCK_ps` |
| GDDR6 | `Channel, BankGroup, Bank, Row, Column` | `bankgroup`, `bank`, `row`, `column`, `channel_width` | `rate`, `nBL`, `nCL`, `nRCDRD`, `nRCDWD`, `nRP`, `nRAS`, `nRC`, `nWR`, `nRTP`, `nCWL`, `nCCDS`, `nCCDL`, `nRRDS`, `nRRDL`, `nWTRS`, `nWTRL`, `nFAW`, `nRFC`, `nRFCpb`, `nRREFD`, `nREFI`, `tCK_ps`, `nRFCab` |
| HBM1 | `Channel, BankGroup, Bank, Row, Column` | `bankgroup`, `bank`, `row`, `column`, `channel_width` | `rate`, `nBL`, `nCL`, `nRCDRD`, `nRCDWR`, `nRP`, `nRAS`, `nRC`, `nWR`, `nRTPL`, `nCWL`, `nCCDS`, `nCCDL`, `nRRDS`, `nRRDL`, `nWTRS`, `nWTRL`, `nFAW`, `nRFC`, `nRFCpb`, `nRREFD`, `nREFI`, `nREFIpb`, `tCK_ps` |
| HBM2 | `Channel, PseudoChannel, BankGroup, Bank, Row, Column` | `pseudochannel`, `bankgroup`, `bank`, `row`, `column`, `channel_width` | `rate`, `nBL`, `nCL`, `nRCDRD`, `nRCDWR`, `nRP`, `nRAS`, `nRC`, `nWR`, `nRTPL`, `nCWL`, `nCCDS`, `nCCDL`, `nRRDS`, `nRRDL`, `nWTRS`, `nWTRL`, `nFAW`, `nRFC`, `nRFCpb`, `nRREFD`, `nREFI`, `nREFIpb`, `tCK_ps` |
| HBM3 | `Channel, PseudoChannel, Sid, BankGroup, Bank, Row, Column` | `pseudochannel`, `sid`, `bankgroup`, `bank`, `row`, `column`, `channel_width` | `rate`, `nBL`, `nCL`, `nRCDRD`, `nRCDWR`, `nRP`, `nRAS`, `nRC`, `nWR`, `nRTP`, `nCWL`, `nCCDS`, `nCCDL`, `nCCDR`, `nRRDS`, `nRRDL`, `nWTRS`, `nWTRL`, `nRTW`, `nFAW`, `nPPD`, `nRFC`, `nRFCpb`, `nRFMab`, `nRFMpb`, `nRREFD`, `nREFI`, `nREFIpb`, `tCK_ps` |
| HBM4 | `Channel, PseudoChannel, Sid, BankGroup, Bank, Row, Column` | `pseudochannel`, `sid`, `bankgroup`, `bank`, `row`, `column`, `channel_width` | `rate`, `nBL`, `nCL`, `nRCDRD`, `nRCDWR`, `nRP`, `nRAS`, `nRC`, `nWR`, `nRTP`, `nCWL`, `nCCDS`, `nCCDL`, `nCCDR`, `nRRDS`, `nRRDL`, `nWTRS`, `nWTRL`, `nRTW`, `nFAW`, `nPPD`, `nRFC`, `nRFCpb`, `nRFMab`, `nRFMpb`, `nRREFD`, `nREFI`, `nREFIpb`, `tCK_ps` |
| LPDDR5 | `Channel, Rank, BankGroup, Bank, Row, Column` | `rank`, `bankgroup`, `bank`, `row`, `column`, `channel_width` | `rate`, `nBL`, `nCL`, `nRCD`, `nRP`, `nRPab`, `nRAS`, `nRC`, `nWR`, `nRTP`, `nCWL`, `nPPD`, `nCCDS`, `nCCDL`, `nCCDS_WR`, `nCCDL_WR`, `nRRDS`, `nRRDL`, `nWTRS`, `nWTRL`, `nFAW`, `nRFC`, `nRFCpb`, `nREFI`, `nREFIpb`, `nWCKPST`, `nCAS`, `nAAD`, `nCS`, `tCK_ps` |

### Preset 总览

| 标准 | Org presets | Timing presets |
| --- | --- | --- |
| DDR3 | `DDR3_1Gb_x4`, `DDR3_1Gb_x8`, `DDR3_1Gb_x16`, `DDR3_2Gb_x4`, `DDR3_2Gb_x8`, `DDR3_2Gb_x16`, `DDR3_4Gb_x4`, `DDR3_4Gb_x8`, `DDR3_4Gb_x16`, `DDR3_8Gb_x4`, `DDR3_8Gb_x8`, `DDR3_8Gb_x16` | `DDR3_800D`, `DDR3_800E`, `DDR3_1066E`, `DDR3_1066F`, `DDR3_1066G`, `DDR3_1333G`, `DDR3_1333H`, `DDR3_1600H`, `DDR3_1600J`, `DDR3_1600K`, `DDR3_1866K`, `DDR3_1866L`, `DDR3_2133L`, `DDR3_2133M` |
| DDR4 | `DDR4_2Gb_x4`, `DDR4_2Gb_x8`, `DDR4_2Gb_x16`, `DDR4_4Gb_x4`, `DDR4_4Gb_x8`, `DDR4_4Gb_x16`, `DDR4_8Gb_x4`, `DDR4_8Gb_x8`, `DDR4_8Gb_x16`, `DDR4_16Gb_x4`, `DDR4_16Gb_x8`, `DDR4_16Gb_x16` | `DDR4_1600J`, `DDR4_1600K`, `DDR4_1600L`, `DDR4_1866L`, `DDR4_1866M`, `DDR4_1866N`, `DDR4_2133N`, `DDR4_2133P`, `DDR4_2133R`, `DDR4_2400P`, `DDR4_2400R`, `DDR4_2400U`, `DDR4_2400T`, `DDR4_2666T`, `DDR4_2666U`, `DDR4_2666V`, `DDR4_2666W`, `DDR4_2933V`, `DDR4_2933W`, `DDR4_2933Y`, `DDR4_2933AA`, `DDR4_3200W`, `DDR4_3200AA`, `DDR4_3200AC` |
| DDR5 | `DDR5_8Gb_x4`, `DDR5_8Gb_x8`, `DDR5_8Gb_x16`, `DDR5_16Gb_x4`, `DDR5_16Gb_x8`, `DDR5_16Gb_x16`, `DDR5_32Gb_x4`, `DDR5_32Gb_x8`, `DDR5_32Gb_x16` | `DDR5_3200AN`, `DDR5_3200BN`, `DDR5_3200C`, `DDR5_4800AN`, `DDR5_4800BN`, `DDR5_4800C`, `DDR5_5600AN`, `DDR5_6400AN` |
| GDDR6 | `GDDR6_8Gb_x8`, `GDDR6_8Gb_x16`, `GDDR6_16Gb_x8`, `GDDR6_16Gb_x16`, `GDDR6_32Gb_x8`, `GDDR6_32Gb_x16` | `GDDR6_2000_1350mV_double`, `GDDR6_2000_1250mV_double`, `GDDR6_2000_1350mV_quad`, `GDDR6_2000_1250mV_quad` |
| HBM1 | `HBM1_1Gb`, `HBM1_2Gb`, `HBM1_4Gb` | `HBM1_1Gbps`, `HBM1_2Gbps` |
| HBM2 | `HBM2_1Gb`, `HBM2_2Gb`, `HBM2_4Gb`, `HBM2_8Gb` | `HBM2_1600Mbps`, `HBM2_2000Mbps`, `HBM2_2400Mbps` |
| HBM3 | `HBM3_4Gb`, `HBM3_8Gb_8hi`, `HBM3_16Gb_8hi`, `HBM3_32Gb_8hi`, `HBM3_32Gb_16hi` | `HBM3_6400Mbps` |
| HBM4 | `HBM4_32Gb_4Hi`, `HBM4_32Gb_8Hi`, `HBM4_32Gb_16Hi` | `HBM4_8000Mbps`, `HBM4_16000Mbps` |
| LPDDR5 | `LPDDR5_8Gb_x16`, `LPDDR5_16Gb_x16` | `LPDDR5_6400` |

### 常用 Override 说明

| 参数 | 位置 | 说明 |
| --- | --- | --- |
| `rate` | `DRAM.timing.rate` | 数据速率，单位通常是 Mbps/pin。理论带宽主要由 `rate * channel_width * channel/pseudochannel` 决定。 |
| `tCK_ps` | `DRAM.timing.tCK_ps` | 时钟周期，单位 ps。修改 `rate` 时建议同步修改。 |
| `channel_width` | `DRAM.org.channel_width` | 单个 channel 或 pseudo-channel 的数据宽度。HBM preset 通常为 32 bit pseudo-channel。 |
| `rank` | `DRAM.org.rank` | DDR/LPDDR 的 rank 数，影响容量和地址层级。 |
| `pseudochannel` | `DRAM.org.pseudochannel` | HBM 的 pseudo-channel 数。当前 HBM2/HBM3/HBM4 preset 默认是 2。 |
| `sid` | `DRAM.org.sid` | HBM3/HBM4 的 stack id 或 stack-internal dimension，影响容量层级。 |
| `bankgroup`, `bank` | `DRAM.org.bankgroup`, `DRAM.org.bank` | bank 组织，影响 bank-level parallelism 和地址映射空间。 |
| `row`, `column` | `DRAM.org.row`, `DRAM.org.column` | 行列规模，影响容量和 row-hit 行为。 |
| `nCL`, `nRCD*`, `nRP`, `nRAS`, `nRC` | `DRAM.timing.*` | 读延迟、行激活/预充电/行周期等核心时序。 |
| `nCCDS`, `nCCDL`, `nRRDS`, `nRRDL`, `nWTRS`, `nWTRL` | `DRAM.timing.*` | 同/跨 bank group 的列命令、激活和读写切换间隔。 |
| `nRFC`, `nREFI`, `nRFCpb`, `nREFIpb` | `DRAM.timing.*` | refresh 相关时序。当前部分配置使用 `NoRefresh` 时这些参数不会成为主要瓶颈。 |
