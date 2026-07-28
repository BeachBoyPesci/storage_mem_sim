"""Tests for MQSim trace generation and cross-validation.

Core coverage:
  - Trace generation correctness: various addr/size/page combos
  - Sector-alignment expansion (BUG-1 regression)
  - Merge / slice behaviour
  - XML config loading (basic geometry + error paths)
  - Native vs Binary MQSim cross-validation (when engines are available)
"""

import os
import shutil
import tempfile
import unittest
import sys
from unittest import mock

from ..memory_type import MemoryRequestType
from ..memory_config import MemoryEngineConfig
from ..memory_object import MemoryObject
from ..memory_request import MemoryRequest
from ..media import MediaConfig, MediaSystemBackend, MQSimMediaSystem


# ======================================================================
# Helpers
# ======================================================================

def _req(addr, size, req_type):
    """Shortcut: create a MemoryRequest for testing."""
    obj = MemoryObject(addr, size, req_type, MemoryEngineConfig())
    return MemoryRequest(memory_object=obj)


def _cfg(**kw):
    """Shortcut: MediaConfig with MQSim defaults."""
    d = {"media_type": MediaSystemBackend.MQSIM,
         "ssd_config_path": "", "workload_config_path": "",
         "request_size_bytes": 131072, "merge_contiguous": True}
    d.update(kw)
    return MediaConfig(**d)


# ======================================================================
# Trace generation — core behaviour
# ======================================================================

class TestTraceGeneration(unittest.TestCase):
    """Trace output for key addr/size/merge combinations."""

    @classmethod
    def setUpClass(cls):
        from ..media.mqsim_wrapper.pymqsim.trace import load_from_ssdconfig_xml
        cfg = os.path.join(
            os.path.dirname(__file__), "..", "configs", "mqsim",
            "default_ssdconfig.xml",
        )
        load_from_ssdconfig_xml(cfg)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mqsim_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------

    def _write(self, reqs, merge=True, req_size=8192):
        from ..media.mqsim_wrapper.pymqsim import write_trace_file, TraceSliceConfig
        cfg = TraceSliceConfig(merge_contiguous=merge, request_size=req_size)
        path = os.path.join(self.tmp, "trace.txt")
        return write_trace_file(reqs, path, cfg) + (path,)

    def _read_trace(self, path):
        with open(path) as f:
            return [ln.strip() for ln in f]

    # -- basic I/O -----------------------------------------------------

    def test_single_read(self):
        """addr=0, 512B → 1 line, lba=0, sectors=1, type=1."""
        total, lines, path = self._write([_req(0, 512, MemoryRequestType.KREAD)])
        self.assertEqual(total, 512)
        self.assertEqual(lines, 1)
        self.assertEqual(self._read_trace(path), ["0 0 0 1 1"])

    def test_single_write(self):
        """addr=0, 512B write → type=0."""
        _, _, path = self._write([_req(0, 512, MemoryRequestType.KWRITE)])
        self.assertEqual(self._read_trace(path), ["0 0 0 1 0"])

    # -- merge ---------------------------------------------------------

    def test_merge_contiguous_reads(self):
        """2×4KB contiguous reads → merged to 8KB → 1 line."""
        reqs = [_req(0, 4096, MemoryRequestType.KREAD),
                _req(4096, 4096, MemoryRequestType.KREAD)]
        total, lines, path = self._write(reqs, merge=True)
        self.assertEqual(total, 8192)
        self.assertEqual(lines, 1)
        self.assertEqual(self._read_trace(path), ["0 0 0 16 1"])

    def test_gap_prevents_merge(self):
        """addr gap → no merge → 2 lines."""
        reqs = [_req(0, 4096, MemoryRequestType.KREAD),
                _req(16384, 4096, MemoryRequestType.KREAD)]
        _, lines, _ = self._write(reqs, merge=True)
        self.assertEqual(lines, 2)

    def test_mixed_type_not_merged(self):
        """Contiguous read+write kept separate."""
        reqs = [_req(0, 4096, MemoryRequestType.KREAD),
                _req(4096, 4096, MemoryRequestType.KWRITE)]
        _, lines, _ = self._write(reqs, merge=True)
        self.assertEqual(lines, 2)

    def test_merge_preserves_input_order(self):
        """Merging must not sort addresses or group reads before writes."""
        reqs = [
            _req(8192, 4096, MemoryRequestType.KWRITE),
            _req(0, 4096, MemoryRequestType.KREAD),
            _req(4096, 4096, MemoryRequestType.KREAD),
            _req(12288, 4096, MemoryRequestType.KWRITE),
        ]
        _, lines, path = self._write(reqs, merge=True)
        self.assertEqual(lines, 3)
        self.assertEqual(
            self._read_trace(path),
            [
                "0 0 16 8 0",
                "0 1 0 16 1",
                "0 2 24 8 0",
            ],
        )

    # -- slicing -------------------------------------------------------

    def test_large_request_sliced(self):
        """32KB → sliced by 8KB → 4 lines."""
        total, lines, _ = self._write(
            [_req(0, 32768, MemoryRequestType.KREAD)], merge=True)
        self.assertEqual(total, 32768)
        self.assertEqual(lines, 4)

    def test_contiguous_merge_then_slice(self):
        """3 contiguous 8KB reads → merge to 24KB → 3×8KB lines."""
        reqs = [_req(i * 8192, 8192, MemoryRequestType.KREAD) for i in range(3)]
        total, lines, _ = self._write(reqs, merge=True)
        self.assertEqual(total, 24576)
        self.assertEqual(lines, 3)

    # -- no-merge mode -------------------------------------------------

    def test_no_merge_sliced_independently(self):
        """No-merge: each request sliced independently."""
        reqs = [_req(0, 10240, MemoryRequestType.KREAD),
                _req(50000, 10240, MemoryRequestType.KREAD)]
        total, lines, _ = self._write(reqs, merge=False)
        # addr=50000 not sector-aligned → expanded by 1 tail sector
        self.assertEqual(total, 20992)
        self.assertEqual(lines, 4)  # two 10KB → 8KB+2KB each

    # -- sector alignment (BUG-1 regression guards) --------------------

    def test_unaligned_addr_expands_range(self):
        """addr=100 (not 512-aligned), 4KB → expanded to 9 sectors (4608B)."""
        total, lines, _ = self._write(
            [_req(100, 4096, MemoryRequestType.KREAD)], merge=True)
        self.assertEqual(total, 4608)
        self.assertEqual(lines, 1)

    def test_unaligned_addr_multi_line(self):
        """addr=100, 16KB, sliced @4KB → 5 lines (was 4 before BUG-1 fix)."""
        total, lines, _ = self._write(
            [_req(100, 16384, MemoryRequestType.KREAD)], merge=True, req_size=4096)
        self.assertEqual(total, 16896)
        self.assertEqual(lines, 5)

    def test_aligned_addr_unchanged(self):
        """Sector-aligned addr → no expansion (regression guard)."""
        total, lines, _ = self._write(
            [_req(0, 4096, MemoryRequestType.KREAD)], merge=True)
        self.assertEqual(total, 4096)
        self.assertEqual(lines, 1)


# ======================================================================
# XML config loading — basic geometry + error paths
# ======================================================================

class TestConfigLoading(unittest.TestCase):
    """NAND geometry from ssdconfig.xml — one representative config."""

    @classmethod
    def setUpClass(cls):
        d = os.path.join(
            os.path.dirname(__file__), "..", "configs", "mqsim"
        )
        cls._ssd = os.path.join(d, "default_ssdconfig.xml")

    def setUp(self):
        from ..media.mqsim_wrapper.pymqsim import trace as C
        self.C = C
        C._loaded = False

    def test_load_default_geometry(self):
        """Parse default_ssdconfig.xml — verify key geometry values."""
        C = self.C
        loaded = C.load_from_ssdconfig_xml(self._ssd)
        self.assertIn("CHANNELS", loaded)
        self.assertEqual(C.CHANNELS, 8)
        self.assertEqual(C.PAGE_SIZE_BYTES, 8192)
        self.assertEqual(C.SECTORS_PER_PAGE, 16)
        self.assertEqual(C.TOTAL_PLANES, 128)
        self.assertTrue(C._loaded)

    def test_unloaded_raises(self):
        """Functions raise RuntimeError before XML is loaded."""
        C = self.C
        C._loaded = False
        with self.assertRaises(RuntimeError):
            C.align_lba(0)

    def test_theory_functions(self):
        """theory_iops / theory_bw produce sensible results; U grows with size."""
        C = self.C
        from ..media.mqsim_wrapper import pymqsim
        from ..media.mqsim_wrapper.pymqsim import performance_model as model

        C.load_from_ssdconfig_xml(self._ssd)
        self.assertFalse(hasattr(C, "theory_iops"))
        self.assertIs(pymqsim.theory_iops, model.theory_iops)
        u4k = model.theory_bus_utilization(4096)
        u128k = model.theory_bus_utilization(131072)
        self.assertGreater(model.theory_iops(4096), 0)
        self.assertGreater(model.theory_bandwidth_mbps(131072), 0)
        # Larger requests → higher bus utilisation
        self.assertGreater(u128k, u4k)
        # 128KB should be bandwidth-bound
        self.assertGreater(u128k, 0.80, "128KB should be BW-bound")

    def test_rate_above_mqsim_timing_limit_raises(self):
        """MQSim's integer-ns channel period cannot represent >1000 MB/s."""
        with open(self._ssd, encoding="utf-8") as src:
            xml = src.read().replace(
                "<Channel_Transfer_Rate>333</Channel_Transfer_Rate>",
                "<Channel_Transfer_Rate>1001</Channel_Transfer_Rate>",
            )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", encoding="utf-8", delete=False
        ) as dst:
            dst.write(xml)
            invalid_path = dst.name
        try:
            with self.assertRaisesRegex(
                ValueError, "Channel_Transfer_Rate must be <= 1000"
            ):
                self.C.load_from_ssdconfig_xml(invalid_path)
        finally:
            os.remove(invalid_path)

    def test_theory_rejects_non_positive_request_size(self):
        from ..media.mqsim_wrapper.pymqsim import performance_model as model

        self.C.load_from_ssdconfig_xml(self._ssd)
        with self.assertRaises(ValueError):
            model.theory_iops(0)
        with self.assertRaises(ValueError):
            model.theory_bus_utilization(-1)


# ======================================================================
# Error handling
# ======================================================================

class TestMQSimErrors(unittest.TestCase):
    """Graceful behaviour when MQSim native module is absent."""

    def setUp(self):
        self.sys = MQSimMediaSystem(_cfg(bandwidth=3.5))

    def test_empty_request_list(self):
        """Empty input → zero metrics."""
        m = self.sys.handler_mem_request([])
        self.assertEqual(m.num_media_reqs, 0)
        self.assertEqual(m.time, 0.0)

    def test_raises_when_mqsim_not_built(self):
        """RuntimeError with build instructions when _mqsim unavailable."""
        self.sys._mqsim_ready = False
        with self.assertRaises(RuntimeError) as ctx:
            self.sys.handler_mem_request(
                [_req(0, 512, MemoryRequestType.KREAD)])
        self.assertIn("_mqsim", str(ctx.exception))

    def test_explicit_missing_ssd_config_raises(self):
        with self.assertRaisesRegex(FileNotFoundError, "SSD config not found"):
            MQSimMediaSystem(_cfg(ssd_config_path="/missing/ssdconfig.xml"))

    def test_explicit_missing_workload_config_raises(self):
        with self.assertRaisesRegex(
            FileNotFoundError, "Workload config not found"
        ):
            MQSimMediaSystem(
                _cfg(workload_config_path="/missing/workload.xml")
            )

    def test_incomplete_native_run_rejects_reported_iops(self):
        """Generated-only XML IOPS is invalid if requests did not complete."""
        from ..media.mqsim_wrapper.pymqsim import simulator

        class IncompleteNative:
            @staticmethod
            def run_with_stats(*args):
                return {
                    "generated_request_count": 2,
                    "serviced_request_count": 1,
                }

        with tempfile.TemporaryDirectory(prefix="mqsim_incomplete_") as tmp:
            ssd = os.path.join(tmp, "input_ssd.xml")
            workload = os.path.join(tmp, "input_workload.xml")
            for path in (ssd, workload):
                with open(path, "w", encoding="utf-8") as output:
                    output.write("<root/>")

            with mock.patch.object(
                simulator, "_get_native", return_value=IncompleteNative()
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "generated=2, serviced=1"
                ):
                    simulator.run_simulation(
                        ssd,
                        workload,
                        output_dir=os.path.join(tmp, "output"),
                    )


# ======================================================================
# Native vs Binary cross-validation (requires both engines)
# ======================================================================

_mqsim_ok = False
try:
    from ..media.mqsim_wrapper.pymqsim import check_mqsim_available
    _mqsim_ok = check_mqsim_available()
except Exception:
    pass

_binary_ok = False
try:
    _bin = os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..",
        "media", "mqsim_wrapper", "MQSim", "MQSim"))
    if os.path.isfile(_bin) and os.access(_bin, os.X_OK):
        _binary_ok = True
except Exception:
    pass

_skip_cross = not (_mqsim_ok and _binary_ok)


@unittest.skipIf(_skip_cross, "Both native _mqsim and MQSim binary required")
class TestNativeVsBinary(unittest.TestCase):
    """Cross-validate: native pybind11 ≈ MQSim binary (subprocess)."""

    def setUp(self):
        self.sys = MQSimMediaSystem(_cfg(bandwidth=3.5))
        ssd = (self.sys.config.ssd_config_path
               or os.path.join(os.path.dirname(__file__), "..",
                               "configs", "mqsim", "default_ssdconfig.xml"))
        self._ssd_cfg = os.path.abspath(ssd)
        self._trace_dir = os.path.join(
            os.path.dirname(__file__), "..", "media", "mqsim_wrapper", "trace")
        os.makedirs(self._trace_dir, exist_ok=True)

    def tearDown(self):
        for f in os.listdir(self._trace_dir):
            if f.startswith("mqsim_"):
                os.remove(os.path.join(self._trace_dir, f))

    # -- helpers -------------------------------------------------------

    def _gen(self, reqs, name, merge=True, req_size=131072):
        from ..media.mqsim_wrapper.pymqsim import (write_trace_file,
                                                     generate_workload_xml)
        tp = os.path.join(self._trace_dir, f"mqsim_{name}.txt")
        wp = os.path.join(self._trace_dir, f"mqsim_{name}.xml")
        cfg = type(self.sys.trace_config)(
            merge_contiguous=merge, request_size=req_size)
        write_trace_file(reqs, tp, cfg)
        generate_workload_xml(tp, wp)
        return tp, wp

    def _native(self, wl_path):
        from ..media.mqsim_wrapper.pymqsim import run_simulation
        return run_simulation(ssd_config_path=self._ssd_cfg,
                              workload_xml_path=wl_path,
                              output_dir=self._trace_dir)

    def _binary(self, wl_path):
        import subprocess
        from ..media.mqsim_wrapper.pymqsim import parse_mqsim_output
        proc = subprocess.run(
            [_bin, "-i", self._ssd_cfg, "-w", wl_path],
            cwd=self._trace_dir, input=b"\n",
            capture_output=True, timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"MQSim binary exited {proc.returncode}: "
                f"{proc.stderr.decode(errors='replace')[-500:]}")
        return parse_mqsim_output(
            os.path.join(self._trace_dir, "workload_scenario_1.xml"))

    # -- tests ---------------------------------------------------------

    def test_sequential_128kb_matches(self):
        """8×128KB sequential: native BW ≈ binary BW within 5%."""
        n = 8
        reqs = [_req(i * 131072, 131072, MemoryRequestType.KREAD)
                for i in range(n)]
        _, wp = self._gen(reqs, "cmp_128k")
        nr = self._native(wp)
        br = self._binary(wp)

        self.assertGreater(nr.bandwidth_bytes_per_sec, 0)
        self.assertGreater(br.bandwidth_bytes_per_sec, 0)
        r = nr.bandwidth_bytes_per_sec / br.bandwidth_bytes_per_sec
        self.assertAlmostEqual(r, 1.0, delta=0.05,
            msg=f"native={nr.bandwidth_bytes_per_sec/1e9:.2f}GB/s "
                f"binary={br.bandwidth_bytes_per_sec/1e9:.2f}GB/s")

    def test_random_4kb_matches(self):
        """32×4KB random (non-merge): native IOPS ≈ binary IOPS within 5%."""
        reqs = [_req(i * 65536, 4096, MemoryRequestType.KREAD)
                for i in range(32)]
        _, wp = self._gen(reqs, "cmp_4k", merge=False, req_size=4096)
        nr = self._native(wp)
        br = self._binary(wp)

        self.assertGreater(nr.total_iops, 0)
        self.assertGreater(br.total_iops, 0)
        r = nr.total_iops / br.total_iops
        self.assertAlmostEqual(r, 1.0, delta=0.05,
            msg=f"native IOPS={nr.total_iops:.0f} "
                f"binary IOPS={br.total_iops:.0f}")


if __name__ == "__main__":
    unittest.main()
