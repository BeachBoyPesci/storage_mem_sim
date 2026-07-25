"""Analytical MQSim performance bounds derived from loaded SSD geometry.

The model is intentionally separate from trace generation.  Call
``trace.load_from_ssdconfig_xml()`` before using these helpers so the model can
read the active NAND geometry and host-interface limits.
"""

import math

from . import trace as _trace


def theory_iops(request_size_bytes: int) -> float:
    """Return the theoretical maximum IOPS for a fixed request size.

    The result is the tightest of these independent resource bounds:

    1. Channel bus bandwidth:
          iops_bw = TOTAL_CHANNEL_BW_MBPS × 1e6 / S

    2. NAND plane array parallelism:
          iops_nand = TOTAL_PLANES × 1e9 / (pages_per_io × tR)

    3. CWDP pipeline:
          dies_per_ch = CHIPS_PER_CH × DIES_PER_CHIP
          PipeCycle = max(tR, dies_per_ch × BusTime)
          iops_cwdp = TOTAL_DIES × 1e9 / PipeCycle

    4. Host PCIe bandwidth:
          iops_pcie = PCIE_LANE_BW_GBPS × 1e9 × PCIE_LANE_COUNT / S

    5. Device queue depth, using Little's Law:
          per_io_latency_ns = tR + BusTime
          iops_qd = IO_QUEUE_DEPTH × 1e9 / per_io_latency_ns
    """
    _trace._require_loaded()
    if request_size_bytes <= 0:
        raise ValueError(
            f"request_size_bytes must be > 0, got {request_size_bytes}"
        )
    pages_per_io = max(
        1, math.ceil(request_size_bytes / _trace.PAGE_SIZE_BYTES)
    )

    # Bound 1 — pure channel bus bandwidth
    iops_bw = (
        _trace.TOTAL_CHANNEL_BW_MBPS * 1e6 / request_size_bytes
    )

    # Bound 2 — pure NAND plane parallelism
    iops_nand = (
        _trace.TOTAL_PLANES
        / (pages_per_io * _trace.NAND_tR_NS * 1e-9)
    )

    # Bound 3 — CWDP pipeline (channels parallel, dies per ch serial on bus)
    dies_per_ch = _trace.CHIPS_PER_CH * _trace.DIES_PER_CHIP
    data_out_ns = (
        request_size_bytes / 2.0
    ) * (2000.0 / _trace.CHANNEL_BW_MBPS)
    bus_time_ns = (
        _trace.CMD_TRANSFER_NS + _trace.DATA_SETUP_NS + data_out_ns
    )
    pipeline_cycle_ns = max(
        _trace.NAND_tR_NS, dies_per_ch * bus_time_ns
    )
    iops_cwdp = _trace.TOTAL_DIES * 1e9 / pipeline_cycle_ns

    # Bound 4 — host PCIe bandwidth
    iops_pcie = (
        _trace.PCIE_LANE_BW_GBPS
        * 1e9
        * _trace.PCIE_LANE_COUNT
        / request_size_bytes
    )

    # Bound 5 — device queue depth
    iops_qd = (
        _trace.IO_QUEUE_DEPTH
        * 1e9
        / (_trace.NAND_tR_NS + bus_time_ns)
    )

    return min(iops_bw, iops_nand, iops_cwdp, iops_pcie, iops_qd)


def theory_bandwidth_mbps(request_size_bytes: int) -> float:
    """Return theoretical effective bandwidth in decimal MB/s."""
    _trace._require_loaded()
    bandwidth_mbps = theory_iops(request_size_bytes) * request_size_bytes / 1e6
    return min(bandwidth_mbps, float(_trace.TOTAL_CHANNEL_BW_MBPS))


def theory_bus_utilization(request_size_bytes: int) -> float:
    """Return channel data-bus utilization in the CWDP pipeline.

    Values below 0.50 are generally IOPS-bound by NAND read latency and
    command overhead; values above 0.90 are generally bandwidth-bound.
    """
    _trace._require_loaded()
    if request_size_bytes <= 0:
        raise ValueError(
            f"request_size_bytes must be > 0, got {request_size_bytes}"
        )
    dies_per_ch = _trace.CHIPS_PER_CH * _trace.DIES_PER_CHIP
    data_out_ns = (
        request_size_bytes / 2.0
    ) * (2000.0 / _trace.CHANNEL_BW_MBPS)
    pipeline_cycle_ns = max(
        _trace.NAND_tR_NS,
        dies_per_ch
        * (_trace.CMD_TRANSFER_NS + _trace.DATA_SETUP_NS + data_out_ns),
    )
    return dies_per_ch * data_out_ns / pipeline_cycle_ns
