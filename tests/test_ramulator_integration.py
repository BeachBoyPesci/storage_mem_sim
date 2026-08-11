"""Integration tests: our RamulatorMediaSystem vs. direct Ramulator2 API.

Verifies that our wrapper produces the same simulation results as calling
Ramulator2's Python API directly, for identical hardware configs and traces.
"""

import os
import sys
import tempfile
import unittest

_SETUP_MSG = (
    "Ramulator2 Python package is not installed. To install:\n"
    "  git submodule update --init media/ramulator_wrapper/ramulator2\n"
    "  pip install -e media/ramulator_wrapper/ramulator2\n"
    "If the C++ extension has not been built:\n"
    "  cmake -S media/ramulator_wrapper/ramulator2 -B media/ramulator_wrapper/ramulator2/build \\\n"
    "    -DCMAKE_BUILD_TYPE=Release -DRAMULATOR_PYTHON_BINDINGS=ON -DCMAKE_CXX_COMPILER=g++-14\n"
    "  cmake --build media/ramulator_wrapper/ramulator2/build -j$(sysctl -n hw.ncpu)\n"
    "  pip install -e media/ramulator_wrapper/ramulator2\n"
)

try:
    import ramulator  # noqa: F401
    _ramulator_available = True
except ImportError:
    _ramulator_available = False
    sys.stderr.write(f"\n[WARNING] {_SETUP_MSG}\n")

from ..memory_type import MemoryType, MemoryRequestType
from ..memory_config import MemoryEngineConfig
from ..memory_request import MemoryRequest
from ..memory_engine import MemoryEngine
from ..memory_metrics import MemoryMetrics, MemoryEngineMetrics
from ..media import (
    MediaConfig,
    MediaSystemBackend,
    RamulatorMediaSystem,
)


def _require_ramulator():
    if not _ramulator_available:
        raise unittest.SkipTest(_SETUP_MSG)


def _write_trace(path, addrs, ops):
    """Write a LoadStoreTrace file: LD/ST <hex_addr>."""
    with open(path, "w") as f:
        for addr, op in zip(addrs, ops):
            f.write(f"{op} 0x{addr:x}\n")


def _run_direct_simulation(dram_impl, org_preset, timing_preset,
                            controller_class, addrs, ops):
    """Run Ramulator2 directly via its Python API. Returns cycles."""
    dram_cls = getattr(ramulator.dram, dram_impl)
    dram = dram_cls(org_preset=org_preset, timing_preset=timing_preset)

    ctrl_cls = getattr(ramulator.controller, controller_class)
    ctrl = ctrl_cls(
        dram=dram,
        scheduler=ramulator.scheduler.FRFCFS(),
        row_policy=ramulator.row_policy.Open(),
        addr_mapper=ramulator.addr_mapper.RoBaRaCoCh(),
        refresh_manager=ramulator.refresh_manager.NoRefresh(),
    )

    mem = ramulator.memory_system.GenericDRAM(
        clock_ratio=1,
        controllers=[ctrl],
        channel_mapper=ramulator.channel_mapper.CacheLineInterleave(),
    )

    # Write trace
    tmp_dir = tempfile.mkdtemp(prefix="ram_test_")
    trace_path = os.path.join(tmp_dir, "trace.txt")
    _write_trace(trace_path, addrs, ops)

    try:
        frontend = ramulator.frontend.LoadStoreTrace(
            clock_ratio=1, path=trace_path,
        )
        sim = ramulator.Simulation(frontend=frontend, memory_system=mem)
        sim.run()
        ctrl_stats = sim.stats["memory_system"]["controller"]
        return int(ctrl_stats.get("cycles", 0))
    finally:
        try:
            os.remove(trace_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass


class TestDDR5Integration(unittest.TestCase):
    """Compare our wrapper vs. direct Ramulator2 for DDR5."""

    YAML_TEXT = """\
MemorySystem:
  impl: GenericDRAM
  clock_ratio: 1
  ChannelMapper:
    impl: CacheLineInterleave
  DRAM:
    impl: DDR5
    org:
      preset: DDR5_16Gb_x8
    timing:
      preset: DDR5_4800AN
  Controllers:
    - impl: GenericDDR
      Scheduler:
        impl: FRFCFS
      RowPolicy:
        impl: Open
      AddrMapper:
        impl: RoBaRaCoCh
      RefreshManager:
        impl: NoRefresh
"""

    def setUp(self):
        _require_ramulator()

    def _make_addrs(self, tx_bytes, count, stride=1):
        return [i * tx_bytes * stride for i in range(count)]

    def test_ddr5_tx_bytes_matches(self):
        """DDR5 _tx_bytes matches Ramulator2's C++ get_tx_bytes() formula."""
        import ramulator
        media_cfg = MediaConfig(
            media_type=MediaSystemBackend.RAMULATOR,
        )
        wrapper = RamulatorMediaSystem(media_cfg)
        wrapper._yaml_text = self.YAML_TEXT
        wrapper._init_ramulator()

        # C++ formula: get_tx_bytes() = internal_prefetch_size * channel_width / 8
        dram = ramulator.dram.DDR5(org_preset="DDR5_16Gb_x8",
                                   timing_preset="DDR5_4800AN")
        org, _ = dram.resolve()
        expected = dram.internal_prefetch_size * org["channel_width"] // 8
        self.assertEqual(wrapper._tx_bytes, expected,
                         f"DDR5 tx_bytes mismatch: wrapper={wrapper._tx_bytes}, expected={expected}")

    def test_ddr5_16_reads(self):
        """DDR5: 16 reads, same config → same cycles."""
        media_cfg = MediaConfig(
            media_type=MediaSystemBackend.RAMULATOR,
        )
        wrapper = RamulatorMediaSystem(media_cfg)
        wrapper._yaml_text = self.YAML_TEXT
        wrapper._init_ramulator()
        g = wrapper._tx_bytes

        addrs = self._make_addrs(g, 16, stride=4)  # spread out
        ops = ["LD"] * 16

        # Our wrapper
        mem_cfg = MemoryEngineConfig(media_config=MediaConfig(media_type=MediaSystemBackend.ANALYTIC,bandwidth=100.0,capacity=1.0))
        reqs = [
            MemoryRequest(a, g, MemoryRequestType.KREAD, config=mem_cfg)
            for a in addrs
        ]
        wrapper_metrics = wrapper.handler_mem_request(reqs)

        # Direct Ramulator2
        direct_cycles = _run_direct_simulation(
            "DDR5", "DDR5_16Gb_x8", "DDR5_4800AN",
            "GenericDDR", addrs, ops,
        )

        self.assertEqual(wrapper_metrics.cycles, direct_cycles,
                         f"Wrapper={wrapper_metrics.cycles}, Direct={direct_cycles}")
        self.assertGreater(wrapper_metrics.cycles, 0)

    def test_ddr5_mixed_read_write(self):
        """DDR5: mixed reads and writes."""
        media_cfg = MediaConfig(
            media_type=MediaSystemBackend.RAMULATOR,
        )
        wrapper = RamulatorMediaSystem(media_cfg)
        wrapper._yaml_text = self.YAML_TEXT
        wrapper._init_ramulator()
        g = wrapper._tx_bytes

        addrs = self._make_addrs(g, 8)
        ops = ["LD"] * 4 + ["ST"] * 4

        # Our wrapper
        mem_cfg = MemoryEngineConfig(media_config=MediaConfig(media_type=MediaSystemBackend.ANALYTIC,bandwidth=100.0,capacity=1.0))
        types = [MemoryRequestType.KREAD] * 4 + [MemoryRequestType.KWRITE] * 4
        reqs = [
            MemoryRequest(a, g, t, config=mem_cfg)
            for a, t in zip(addrs, types)
        ]
        wrapper_metrics = wrapper.handler_mem_request(reqs)

        # Direct
        direct_cycles = _run_direct_simulation(
            "DDR5", "DDR5_16Gb_x8", "DDR5_4800AN",
            "GenericDDR", addrs, ops,
        )

        self.assertEqual(wrapper_metrics.cycles, direct_cycles,
                         f"Wrapper={wrapper_metrics.cycles}, Direct={direct_cycles}")

    def test_ddr5_with_yaml_config_file(self):
        """DDR5 via YAML config file → same as default fallback."""
        import yaml
        media_cfg = MediaConfig(
            media_type=MediaSystemBackend.RAMULATOR,
        )

        # Write temp YAML
        tmp = tempfile.mkdtemp(prefix="ram_yaml_")
        yaml_path = os.path.join(tmp, "config.yaml")
        with open(yaml_path, "w") as f:
            f.write(self.YAML_TEXT)

        try:
            media_cfg.config_path = yaml_path
            wrapper = RamulatorMediaSystem(media_cfg)
            g = wrapper._tx_bytes

            addrs = self._make_addrs(g, 8, stride=2)
            ops = ["LD"] * 8

            mem_cfg = MemoryEngineConfig(media_config=MediaConfig(media_type=MediaSystemBackend.ANALYTIC,bandwidth=100.0,capacity=1.0))
            reqs = [
                MemoryRequest(a, g, MemoryRequestType.KREAD, config=mem_cfg)
                for a in addrs
            ]
            wrapper_metrics = wrapper.handler_mem_request(reqs)

            direct_cycles = _run_direct_simulation(
                "DDR5", "DDR5_16Gb_x8", "DDR5_4800AN",
                "GenericDDR", addrs, ops,
            )

            self.assertEqual(wrapper_metrics.cycles, direct_cycles)
        finally:
            os.remove(yaml_path)
            os.rmdir(tmp)


class TestHBM3Integration(unittest.TestCase):
    """Compare our wrapper vs. direct Ramulator2 for HBM3."""

    YAML_TEXT = """\
MemorySystem:
  impl: GenericDRAM
  clock_ratio: 1
  ChannelMapper:
    impl: CacheLineInterleave
  DRAM:
    impl: HBM3
    org:
      preset: HBM3_16Gb_8hi
    timing:
      preset: HBM3_6400Mbps
  Controllers:
    - impl: HBM34
      Scheduler:
        impl: FRFCFS
      RowPolicy:
        impl: Open
      AddrMapper:
        impl: RoBaRaCoCh
      RefreshManager:
        impl: NoRefresh
"""

    def setUp(self):
        _require_ramulator()

    def _make_addrs(self, tx_bytes, count, stride=1):
        return [i * tx_bytes * stride for i in range(count)]

    def test_hbm3_16_reads(self):
        """HBM3: 16 reads, same config → same cycles."""
        media_cfg = MediaConfig(
            media_type=MediaSystemBackend.RAMULATOR,
        )
        wrapper = RamulatorMediaSystem(media_cfg)
        wrapper._yaml_text = self.YAML_TEXT
        wrapper._init_ramulator()
        g = wrapper._tx_bytes

        addrs = self._make_addrs(g, 16, stride=4)
        ops = ["LD"] * 16

        mem_cfg = MemoryEngineConfig(media_config=MediaConfig(media_type=MediaSystemBackend.ANALYTIC,bandwidth=100.0,capacity=1.0))
        reqs = [
            MemoryRequest(a, g, MemoryRequestType.KREAD, config=mem_cfg)
            for a in addrs
        ]
        wrapper_metrics = wrapper.handler_mem_request(reqs)

        direct_cycles = _run_direct_simulation(
            "HBM3", "HBM3_16Gb_8hi", "HBM3_6400Mbps",
            "HBM34", addrs, ops,
        )

        self.assertEqual(wrapper_metrics.cycles, direct_cycles,
                         f"Wrapper={wrapper_metrics.cycles}, Direct={direct_cycles}")
        self.assertGreater(wrapper_metrics.cycles, 0)

    def test_hbm3_32_reads(self):
        """HBM3: larger batch of reads."""
        media_cfg = MediaConfig(
            media_type=MediaSystemBackend.RAMULATOR,
        )
        wrapper = RamulatorMediaSystem(media_cfg)
        wrapper._yaml_text = self.YAML_TEXT
        wrapper._init_ramulator()
        g = wrapper._tx_bytes

        addrs = self._make_addrs(g, 32, stride=2)
        ops = ["LD"] * 32

        mem_cfg = MemoryEngineConfig(media_config=MediaConfig(media_type=MediaSystemBackend.ANALYTIC,bandwidth=100.0,capacity=1.0))
        reqs = [
            MemoryRequest(a, g, MemoryRequestType.KREAD, config=mem_cfg)
            for a in addrs
        ]
        wrapper_metrics = wrapper.handler_mem_request(reqs)

        direct_cycles = _run_direct_simulation(
            "HBM3", "HBM3_16Gb_8hi", "HBM3_6400Mbps",
            "HBM34", addrs, ops,
        )

        self.assertEqual(wrapper_metrics.cycles, direct_cycles,
                         f"Wrapper={wrapper_metrics.cycles}, Direct={direct_cycles}")

    def test_hbm3_tx_bytes_matches(self):
        """HBM3 _tx_bytes matches Ramulator2's C++ get_tx_bytes() formula."""
        import ramulator
        media_cfg = MediaConfig(
            media_type=MediaSystemBackend.RAMULATOR,
        )
        wrapper = RamulatorMediaSystem(media_cfg)
        wrapper._yaml_text = self.YAML_TEXT
        wrapper._init_ramulator()

        # C++ formula: get_tx_bytes() = internal_prefetch_size * channel_width / 8
        dram = ramulator.dram.HBM3(org_preset="HBM3_16Gb_8hi",
                                   timing_preset="HBM3_6400Mbps")
        org, _ = dram.resolve()
        expected = dram.internal_prefetch_size * org["channel_width"] // 8
        self.assertEqual(wrapper._tx_bytes, expected,
                         f"HBM3 tx_bytes mismatch: wrapper={wrapper._tx_bytes}, expected={expected}")


if __name__ == "__main__":
    unittest.main()
