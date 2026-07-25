"""pymqsim — Python library for MQSim SSD simulator.

Quick start:
    from pymqsim import (write_trace_file, TraceSliceConfig,
                         generate_workload_xml, run_simulation)

    cfg = TraceSliceConfig(merge_contiguous=True)
    write_trace_file(mem_req_list, "trace.txt", cfg)

    generate_workload_xml("trace.txt", "workload.xml")

    result = run_simulation("trace.txt", "ssdconfig.xml", "workload.xml")
    print(f"{result.bandwidth_bytes_per_sec / 1e9:.2f} GB/s")
"""

from .trace import (
    # Geometry constants
    CHANNELS, SECTOR_SIZE, PAGE_SIZE_BYTES, SECTORS_PER_PAGE,align_lba,
    addr_to_lba, size_to_sectors,
    # XML loaders
    load_from_ssdconfig_xml, load_from_workload_xml,
    # Trace config
    TraceSliceConfig,
    # Trace generation
    write_trace_file, build_trace_lines, merge_sequential,
)
from .performance_model import (
    theory_iops,
    theory_bandwidth_mbps,
    theory_bus_utilization,
)
from .workload import generate_workload_xml, MQSimWorkload
from .output import MQSimResult, parse_mqsim_output
from .simulator import (
    run_simulation,
    check_mqsim_available,
)

__all__ = [
    # Primary
    "run_simulation",
    "check_mqsim_available",
    "write_trace_file",
    "build_trace_lines",
    "merge_sequential",
    # Trace config
    "TraceSliceConfig",
    # Results
    "MQSimResult",
    "parse_mqsim_output",
    # Workload
    "generate_workload_xml",
    "MQSimWorkload",
    # Geometry constants
    "CHANNELS", "SECTOR_SIZE", "PAGE_SIZE_BYTES", "SECTORS_PER_PAGE",
    "align_lba",
    "addr_to_lba", "size_to_sectors",
    # XML loaders
    "load_from_ssdconfig_xml", "load_from_workload_xml",
    # Theory formulas
    "theory_iops", "theory_bandwidth_mbps", "theory_bus_utilization",
]
