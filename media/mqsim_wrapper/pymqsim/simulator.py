"""MQSim simulation engine — runs the simulator via native pybind11 binding.

This module ONLY runs the simulation — trace generation and workload
XML are handled by trace.py and workload.py respectively.
"""

import os
import logging
from typing import Optional

from .output import MQSimResult, parse_mqsim_output

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Native pybind11 binding
# ------------------------------------------------------------------

_native_module = None


def _get_native():
    global _native_module
    if _native_module is None:
        try:
            from . import _mqsim  # type: ignore[import]
            _native_module = _mqsim
            logger.info("Using native _mqsim pybind11 binding.")
        except ImportError:
            raise RuntimeError(
                "_mqsim pybind11 module not built.\n"
                "Build: cd media/mqsim_wrapper && pip install -e ."
            )
    return _native_module


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def check_mqsim_available() -> bool:
    """Check whether the native _mqsim pybind11 module is available."""
    try:
        _get_native()
        return True
    except RuntimeError:
        return False


def run_simulation(
    ssd_config_path: str,
    workload_xml_path: str,
    *,
    output_dir: Optional[str] = None,
) -> MQSimResult:
    """Run a single MQSim simulation via native pybind11 binding.

    Args:
        ssd_config_path:  Path to ssdconfig.xml.
        workload_xml_path: Path to workload XML (pre-built by caller).
        output_dir:       Working directory for output files.

    Returns:
        MQSimResult.

    Raises:
        FileNotFoundError: Config / workload not found.
        RuntimeError:      Native module not built, or simulation failed.
    """
    for label, p in [("SSD config", ssd_config_path),
                     ("workload XML", workload_xml_path)]:
        if not os.path.isfile(p):
            raise FileNotFoundError(f"{label} not found: {p}")

    if output_dir is None:
        os.makedirs("mqsim_output", exist_ok=True)
        output_dir = os.path.abspath("mqsim_output")
    else:
        os.makedirs(output_dir, exist_ok=True)

    ssd_local = os.path.join(output_dir, "ssdconfig.xml")
    print(f"[MQSim] output dir: {os.path.abspath(output_dir)}")

    _copy_file(ssd_config_path, ssd_local)

    native = _get_native()

    if hasattr(native, 'run_with_stats'):
        stats = native.run_with_stats(
            ssd_local, workload_xml_path, output_dir)
        _validate_completed_requests(stats)
        ok = stats is not None
    else:
        ok = native.run(ssd_local, workload_xml_path, output_dir)

    if not ok:
        raise RuntimeError("MQSim pybind11 simulation failed.")

    # Parse output XML
    output_xml = _find_output_xml(output_dir)
    if output_xml is None:
        raise RuntimeError(
            f"MQSim completed without a workload result XML in {output_dir}"
        )

    print(f"[MQSim] result file: {os.path.abspath(output_xml)}")
    return parse_mqsim_output(output_xml)


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def _validate_completed_requests(stats) -> None:
    """Require every generated MQSim request to reach host completion.

    MQSim calculates XML IOPS from generated requests and final simulator
    time.  A partial run would therefore overstate completed end-to-end IOPS
    unless generated and serviced counts are equal.
    """
    if stats is None:
        return
    generated = stats.get("generated_request_count")
    serviced = stats.get("serviced_request_count")
    if generated is None or serviced is None:
        return
    generated = int(generated)
    serviced = int(serviced)
    if generated != serviced:
        raise RuntimeError(
            "MQSim simulation ended with incomplete requests: "
            f"generated={generated}, serviced={serviced}"
        )


def _copy_file(src: str, dst: str) -> None:
    d = os.path.dirname(dst)
    if d:
        os.makedirs(d, exist_ok=True)
    import shutil
    shutil.copy2(src, dst)


def _find_output_xml(output_dir: str) -> Optional[str]:
    for i in range(1, 9):
        path = os.path.join(output_dir, f"workload_scenario_{i}.xml")
        if os.path.isfile(path):
            return path
    return None
