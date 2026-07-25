"""MQSim media system — thin orchestration layer.

  1. Build trace    →  pymqsim.trace.write_trace_file()
  2. Build workload →  pymqsim.workload.generate_workload_xml()
  3. Run simulation →  pymqsim.simulator.run_simulation()
  4. Return MediaMetrics
"""

import os
import uuid
import logging
from typing import List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory_request import MemoryRequest

from .base_media import BaseMediaSystem
from .media_config import MediaConfig
from .media_metrics import MediaMetrics
from .mqsim_wrapper.pymqsim.trace import TraceSliceConfig, write_trace_file
from .mqsim_wrapper.pymqsim.workload import generate_workload_xml
from .mqsim_wrapper.pymqsim.simulator import run_simulation
from ..memory_type import MemoryRequestType

logger = logging.getLogger(__name__)

_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "configs", "mqsim"
)
_DEFAULT_SSD_CONFIG = os.path.join(_CONFIG_DIR, "default_ssdconfig.xml")
_DEFAULT_WORKLOAD_CONFIG = os.path.join(_CONFIG_DIR, "default_workload.xml")


class MQSimMediaSystem(BaseMediaSystem):
    """SSD simulation backend — orchestrates trace → workload → simulate."""

    def __init__(self, config: MediaConfig):
        super().__init__(config)
        self._mqsim_ready: bool = False
        self._last_result: Optional[object] = None
        self._ssd_config_path: str = ""
        self._workload_config_path: str = ""
        self._trace_config: TraceSliceConfig = TraceSliceConfig()
        self._init_mqsim()

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def _init_mqsim(self):
        # Explicit paths are part of the experiment definition.  Silently
        # replacing a typo with a default would produce valid-looking results
        # for the wrong device.
        ssd = self.config.ssd_config_path
        if ssd:
            if not os.path.isfile(ssd):
                raise FileNotFoundError(f"SSD config not found: {ssd}")
        else:
            ssd = _DEFAULT_SSD_CONFIG
        self._ssd_config_path = os.path.abspath(ssd)

        wl = self.config.workload_config_path
        if wl:
            if not os.path.isfile(wl):
                raise FileNotFoundError(f"Workload config not found: {wl}")
        else:
            wl = _DEFAULT_WORKLOAD_CONFIG
        self._workload_config_path = os.path.abspath(wl)

        # ---- auto-configure trace slicing from MediaConfig ----
        self._trace_config = TraceSliceConfig(
            merge_contiguous=self.config.merge_contiguous,
            request_size=self.config.request_size_bytes,
        )

        # ---- load NAND geometry from SSD config XML ----
        if os.path.isfile(self._ssd_config_path):
            from .mqsim_wrapper.pymqsim.trace import (
                load_from_ssdconfig_xml, load_from_workload_xml,
            )
            loaded = load_from_ssdconfig_xml(self._ssd_config_path)
            logger.info("Loaded NAND geometry from %s: %s",
                        self._ssd_config_path,
                        ", ".join(f"{k}={v}" for k, v in loaded.items()))
            res = load_from_workload_xml(self._workload_config_path)
            logger.debug("Workload resource IDs: %s",
                         {k: v for k, v in res.items() if v})

        # check native pybind11 availability
        try:
            from .mqsim_wrapper.pymqsim import _mqsim  # noqa: F401
            self._mqsim_ready = True
        except ImportError:
            self._mqsim_ready = False

    # -- trace config ------------------------------------------------

    @property
    def trace_config(self) -> TraceSliceConfig:
        return self._trace_config

    @trace_config.setter
    def trace_config(self, cfg: TraceSliceConfig):
        self._trace_config = cfg

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def merge_sequential(
        self, mem_req_list: List["MemoryRequest"]
    ) -> Tuple[List[int], List[int], List[int]]:
        from .mqsim_wrapper.pymqsim.trace import merge_sequential as _m
        return _m(mem_req_list)

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def handler_mem_request(
        self, mem_req_list: List["MemoryRequest"]
    ) -> MediaMetrics:
        """Orchestrate: trace → workload → simulate → metrics."""
        num_read = sum(
            1 for mr in mem_req_list
            if mr.memory_object.req_type == MemoryRequestType.KREAD)
        num_write = len(mem_req_list) - num_read

        if not mem_req_list:
            m = MediaMetrics()
            self.system_metrics.update_from_media(m)
            return m

        # output directory — unique sub-dir per run to isolate concurrent calls
        trace_root = os.path.join(
            os.path.dirname(__file__), "mqsim_wrapper", "trace")
        rid = uuid.uuid4().hex[:12]
        trace_dir = os.path.join(trace_root, rid)
        os.makedirs(trace_dir, exist_ok=True)

        trace_path = os.path.join(trace_dir, "trace.txt")
        workload_path = os.path.join(trace_dir, "workload.xml")

        # 1. write trace
        total_bytes, trace_lines = write_trace_file(
            mem_req_list, trace_path, self._trace_config)

        # 2. write workload XML (replace File_Path in template)
        generate_workload_xml(trace_path, workload_path,
                              self._workload_config_path)

        print(f"[MQSim] trace:    {os.path.abspath(trace_path)}  "
              f"({trace_lines} lines)")
        print(f"[MQSim] workload: {os.path.abspath(workload_path)}")

        # 3. run simulation (native pybind11)
        if not self._mqsim_ready:
            raise RuntimeError(
                "MQSim native module (_mqsim) not built.\n"
                "Build: cd media/mqsim_wrapper && pip install -e ."
            )
        result = run_simulation(
            ssd_config_path=self._ssd_config_path,
            workload_xml_path=workload_path,
            output_dir=trace_dir,
        )
        self._last_result = result

        total_time = 0.0
        if result.total_time_s > 0:
            total_time = result.total_time_s
        elif result.bandwidth_bytes_per_sec > 0:
            total_time = total_bytes / result.bandwidth_bytes_per_sec

        logger.info("MQSim: %.1f s total_time, %.2f GB/s, %.0f IOPS",
                    total_time,
                    result.bandwidth_bytes_per_sec / (1024**3),
                    result.total_iops)

        metrics = MediaMetrics(
            num_read_requests=num_read,
            num_write_requests=num_write,
            num_media_reqs=trace_lines,
            time=total_time,
            bandwidth=result.bandwidth_bytes_per_sec,
            iops=result.total_iops,
            iops_read=result.iops_read,
            iops_write=result.iops_write,
        )
        self.system_metrics.update_from_media(metrics)
        return metrics

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def last_result(self):
        return self._last_result

    @property
    def mqsim_available(self) -> bool:
        return self._mqsim_ready
