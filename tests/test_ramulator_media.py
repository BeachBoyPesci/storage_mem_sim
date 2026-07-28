"""Tests for RamulatorMediaSystem — cycle-accurate DRAM simulation backend.

The Ramulator2 Python package must be built and on sys.path:
    pip install -e media/ramulator_wrapper
"""

import unittest
import sys
import os

from ..memory_type import MemoryType, MemoryRequestType
from ..memory_config import MemoryEngineConfig
from ..memory_object import MemoryObject
from ..memory_request import MemoryRequest
from ..memory_engine import MemoryEngine
from ..memory_metrics import MemoryMetrics, MemoryEngineMetrics
from ..media import (
    MediaConfig,
    MediaSystemBackend,
    RamulatorMediaSystem,
    MediaMetrics,
)


_SETUP_MSG = (
    "Ramulator2 Python package is not installed. To install:\n"
    "  pip install -e media/ramulator_wrapper\n"
)
_ramulator_available = False
try:
    import ramulator  # noqa: F401
    _ramulator_available = True
except ImportError:
    import sys
    sys.stderr.write(f"\n[WARNING] {_SETUP_MSG}\n")


def _make_ramulator():
    """Create a RamulatorMediaSystem, skipping with install instructions."""
    if not _ramulator_available:
        raise unittest.SkipTest(_SETUP_MSG)
    return RamulatorMediaSystem(MediaConfig(
        media_type=MediaSystemBackend.RAMULATOR,
        
    ))


class TestRamulatorMediaSystemDecomposition(unittest.TestCase):
    """Test MediaRequest decomposition."""

    def setUp(self):
        self.system = _make_ramulator()
        self.mem_config = MemoryEngineConfig()
        self.g = self.system._tx_bytes

    def _make_memory_request(self, addr, size, req_type):
        obj = MemoryObject(addr, size, req_type, self.mem_config)
        return MemoryRequest(memory_object=obj)

    def test_create_media_requests_single(self):
        req = self._make_memory_request(0, self.g * 2, MemoryRequestType.KREAD)
        media_reqs = self.system.create_media_requests([req])
        self.assertEqual(len(media_reqs), 2)
        self.assertEqual(media_reqs[0].addr, 0)
        self.assertEqual(media_reqs[1].addr, self.g)

    def test_create_media_requests_odd_size(self):
        req = self._make_memory_request(0, self.g + 1, MemoryRequestType.KWRITE)
        media_reqs = self.system.create_media_requests([req])
        self.assertEqual(len(media_reqs), 2)

    def test_create_media_requests_multiple(self):
        reqs = [
            self._make_memory_request(0, self.g, MemoryRequestType.KREAD),
            self._make_memory_request(128, self.g * 3, MemoryRequestType.KWRITE),
        ]
        media_reqs = self.system.create_media_requests(reqs)
        self.assertEqual(len(media_reqs), 1 + 3)

    def test_create_media_requests_address_increment(self):
        req = self._make_memory_request(256, self.g * 4, MemoryRequestType.KREAD)
        media_reqs = self.system.create_media_requests([req])
        addrs = [mr.addr for mr in media_reqs]
        self.assertEqual(addrs, [256, 256+self.g, 256+2*self.g, 256+3*self.g])


class TestRamulatorMediaSystemTraceFormat(unittest.TestCase):
    """Test LoadStoreTrace file format."""

    def setUp(self):
        self.system = _make_ramulator()
        self.mem_config = MemoryEngineConfig()

    def _make_memory_request(self, addr, size, req_type):
        obj = MemoryObject(addr, size, req_type, self.mem_config)
        return MemoryRequest(memory_object=obj)

    def test_trace_format_load_store(self):
        import tempfile
        reqs = [
            self._make_memory_request(0, self.system._tx_bytes, MemoryRequestType.KREAD),
            self._make_memory_request(4096, self.system._tx_bytes, MemoryRequestType.KWRITE),
        ]
        media_reqs = self.system.create_media_requests(reqs)

        tmp = tempfile.mkdtemp()
        tp = os.path.join(tmp, "trace.txt")
        self.system._write_trace_file(media_reqs, tp)

        with open(tp) as f:
            lines = f.read().strip().split("\n")
        os.remove(tp)
        os.rmdir(tmp)

        self.assertEqual(lines[0], "LD 0x0")
        self.assertEqual(lines[1], "ST 0x1000")


class TestRamulatorMediaSystemFull(unittest.TestCase):
    """End-to-end test with actual Ramulator2 simulation."""

    def setUp(self):
        self.system = _make_ramulator()
        self.mem_config = MemoryEngineConfig()

    def _make_memory_request(self, addr, size, req_type):
        obj = MemoryObject(addr, size, req_type, self.mem_config)
        return MemoryRequest(memory_object=obj)

    def test_handler_mem_request_empty(self):
        metrics = self.system.handler_mem_request([])
        self.assertEqual(metrics.num_media_reqs, 0)

    def test_handler_mem_request_single(self):
        req = self._make_memory_request(0, self.system._tx_bytes, MemoryRequestType.KREAD)
        metrics = self.system.handler_mem_request([req])
        self.assertIsInstance(metrics, MediaMetrics)
        self.assertEqual(metrics.num_media_reqs, 1)
        self.assertEqual(metrics.num_read_requests, 1)
        self.assertGreater(metrics.cycles, 0)
        self.assertIsNone(metrics.iops)
        self.assertIsNone(metrics.iops_read)
        self.assertIsNone(metrics.iops_write)

    def test_handler_mem_request_multiple(self):
        g = self.system._tx_bytes
        reqs = [
            self._make_memory_request(0, g, MemoryRequestType.KREAD),
            self._make_memory_request(4096, g * 2, MemoryRequestType.KWRITE),
        ]
        metrics = self.system.handler_mem_request(reqs)
        self.assertEqual(metrics.num_media_reqs, 1 + 2)
        self.assertEqual(metrics.num_read_requests, 1)
        self.assertEqual(metrics.num_write_requests, 2)

    def test_system_metrics_accumulation(self):
        req = self._make_memory_request(0, self.system._tx_bytes, MemoryRequestType.KREAD)
        self.system.handler_mem_request([req])
        self.system.handler_mem_request([req])
        sys_metrics = self.system.get_system_metrics()
        self.assertEqual(sys_metrics.num_media_reqs, 2)
        self.assertEqual(len(sys_metrics.media_metrics_list), 2)

    def test_e2e_ramulator_simulation(self):
        g = self.system._tx_bytes
        reqs = [
            self._make_memory_request(i * g, g, MemoryRequestType.KREAD)
            for i in range(16)
        ]
        metrics = self.system.handler_mem_request(reqs)
        self.assertEqual(metrics.num_read_requests, 16)
        self.assertGreater(metrics.cycles, 0)
        self.assertGreater(metrics.time, 0)


class TestRamulatorTimeConversion(unittest.TestCase):
    """Verify Fix 1: tick_ps from serialized to_config(), time = cycles * tick_ps * 1e-12."""

    def setUp(self):
        from ..media.media_config import MediaConfig
        from ..media.media_backend import MediaSystemBackend
        self.system = _make_ramulator()
        self.fallback_config = MediaConfig(
            media_type=MediaSystemBackend.RAMULATOR,
        )

    def test_tick_ps_is_positive_integer(self):
        """Serialized tCK_ps from to_config() must be a positive integer."""
        self.assertIsInstance(self.system._tick_ps, int)
        self.assertGreater(self.system._tick_ps, 0)

    def test_io_frequency_mhz_derived_from_tick_ps(self):
        """_io_frequency_mhz is derived as 1e6 / _tick_ps (display-only)."""
        expected = 1e6 / self.system._tick_ps
        self.assertAlmostEqual(self.system._io_frequency_mhz, expected, places=0)

    def test_time_from_cycles_and_tick_ps(self):
        """time = cycles * _tick_ps * 1e-12."""
        g = self.system._tx_bytes
        req = self._make_request(0, g, MemoryRequestType.KREAD)
        metrics = self.system.handler_mem_request([req])
        expected_time = metrics.cycles * self.system._tick_ps * 1e-12
        self.assertAlmostEqual(metrics.time, expected_time, places=12)

    def test_hbm3_tick_ps_uses_serialized_value(self):
        """HBM3_6400Mbps: tCK_ps=625 preset, tick_multiplier=2 → 312 ps/tick."""
        import os, tempfile, yaml
        # Minimal config to verify HBM3 serialized tick_ps
        yaml_text = """\
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
  Controllers:
    - impl: HBM34
      Scheduler: {impl: FRFCFS}
      RowPolicy: {impl: Open}
      AddrMapper: {impl: RoBaRaCoCh}
      RefreshManager: {impl: NoRefresh}
"""
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False)
        tmp.write(yaml_text)
        tmp.close()
        try:
            from ..media.media_config import MediaConfig
            from ..media.media_backend import MediaSystemBackend
            from ..media.ramulator_media_system import RamulatorMediaSystem
            config = MediaConfig(
                media_type=MediaSystemBackend.RAMULATOR,
                config_path=tmp.name,
            )
            sys_ = RamulatorMediaSystem(config)
            self.assertEqual(sys_._tick_ps, 312,
                             f"HBM3_6400Mbps should serialize to 312 ps/tick (625//2), got {sys_._tick_ps}")
        finally:
            os.unlink(tmp.name)

    def _make_request(self, addr, size, req_type):
        from ..memory_object import MemoryObject
        from ..memory_request import MemoryRequest
        from ..memory_config import MemoryEngineConfig
        obj = MemoryObject(addr, size, req_type, MemoryEngineConfig())
        return MemoryRequest(memory_object=obj)


class TestCreateComponentParams(unittest.TestCase):
    """Verify Fix 4: _create_component forwards kwargs, rejects unknowns."""

    def setUp(self):
        try:
            import ramulator  # noqa: F401
        except ImportError:
            raise unittest.SkipTest(_SETUP_MSG)

    def test_forward_interleave_bits(self):
        from ..media.ramulator_media_system import _create_component
        import ramulator
        cm = _create_component(ramulator.channel_mapper,
                               {"impl": "CacheLineInterleave", "interleave_bits": 5},
                               "CacheLineInterleave")
        self.assertEqual(cm.interleave_bits, 5)

    def test_default_when_no_params(self):
        from ..media.ramulator_media_system import _create_component
        import ramulator
        cm = _create_component(ramulator.channel_mapper, {}, "CacheLineInterleave")
        self.assertEqual(cm.interleave_bits, 0)

    def test_bad_impl_raises_valueerror(self):
        from ..media.ramulator_media_system import _create_component
        import ramulator
        with self.assertRaises(ValueError) as ctx:
            _create_component(ramulator.channel_mapper, {"impl": "NoSuchThing"}, "CacheLineInterleave")
        self.assertIn("NoSuchThing", str(ctx.exception))

    def test_unknown_param_raises_valueerror(self):
        from ..media.ramulator_media_system import _create_component
        import ramulator
        with self.assertRaises(ValueError) as ctx:
            _create_component(ramulator.channel_mapper,
                              {"impl": "CacheLineInterleave", "typo_param": 42},
                              "CacheLineInterleave")
        self.assertIn("typo_param", str(ctx.exception))


class TestControllerParamForwarding(unittest.TestCase):
    """Verify Fix 4: controller-level params forwarded to ctrl_cls()."""

    def setUp(self):
        try:
            import ramulator  # noqa: F401
        except ImportError:
            raise unittest.SkipTest(_SETUP_MSG)

    def test_controller_buffer_size_reaches_component(self):
        """read_buffer_size from YAML reaches the constructed controller."""
        import yaml, ramulator, tempfile, os
        from ..media.ramulator_media_system import (
            _build_dram, _find_dram, _expand_controller_configs, _create_component,
        )
        yaml_text = """\
MemorySystem:
  impl: GenericDRAM
  clock_ratio: 1
  ChannelMapper: {impl: CacheLineInterleave}
  DRAM: {impl: DDR5, org: {preset: DDR5_16Gb_x8}, timing: {preset: DDR5_4800AN}}
  Controllers:
    - impl: GenericDDR
      read_buffer_size: 64
      write_buffer_size: 64
      Scheduler: {impl: FRFCFS}
      RowPolicy: {impl: Open}
      AddrMapper: {impl: RoBaRaCoCh}
      RefreshManager: {impl: NoRefresh}
"""
        cfg = yaml.safe_load(yaml_text)
        ms = cfg["MemorySystem"]
        dram = _build_dram(_find_dram(cfg))
        ctrl_list = _expand_controller_configs(ms.get("Controllers"))
        c_cfg = ctrl_list[0]

        sched = _create_component(ramulator.scheduler, c_cfg.get("Scheduler", {}), "FRFCFS")
        rp = _create_component(ramulator.row_policy, c_cfg.get("RowPolicy", {}), "Open")
        am = _create_component(ramulator.addr_mapper, c_cfg.get("AddrMapper", {}), "RoBaRaCoCh")
        rm = _create_component(ramulator.refresh_manager, c_cfg.get("RefreshManager", {}), "NoRefresh")

        controller_params = {
            k: v for k, v in c_cfg.items()
            if k not in {"impl", "count", "DRAM", "Scheduler", "RowPolicy",
                         "AddrMapper", "RefreshManager"}
        }
        ctrl_cls = getattr(ramulator.controller, c_cfg["impl"])
        ctrl = ctrl_cls(dram=dram, scheduler=sched, row_policy=rp,
                        addr_mapper=am, refresh_manager=rm, **controller_params)
        self.assertEqual(ctrl.read_buffer_size, 64,
                         f"Expected 64, got {ctrl.read_buffer_size}")
        self.assertEqual(ctrl.write_buffer_size, 64,
                         f"Expected 64, got {ctrl.write_buffer_size}")

    def test_unknown_controller_param_raises_in_handler(self):
        """Unknown controller param raises via the real handler_mem_request path."""
        import tempfile, os
        yaml_text = """\
MemorySystem:
  impl: GenericDRAM
  clock_ratio: 1
  ChannelMapper: {impl: CacheLineInterleave}
  DRAM: {impl: DDR5, org: {preset: DDR5_16Gb_x8}, timing: {preset: DDR5_4800AN}}
  Controllers:
    - impl: GenericDDR
      bad_ctrl_param: 999
      Scheduler: {impl: FRFCFS}
      RowPolicy: {impl: Open}
      AddrMapper: {impl: RoBaRaCoCh}
      RefreshManager: {impl: NoRefresh}
"""
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False)
        tmp.write(yaml_text)
        tmp.close()
        try:
            from ..media.media_config import MediaConfig
            from ..media.media_backend import MediaSystemBackend
            from ..media.ramulator_media_system import RamulatorMediaSystem
            from ..memory_object import MemoryObject
            from ..memory_request import MemoryRequest
            from ..memory_config import MemoryEngineConfig

            config = MediaConfig(
                media_type=MediaSystemBackend.RAMULATOR,
                config_path=tmp.name,
            )
            sys_ = RamulatorMediaSystem(config)
            mem_config = MemoryEngineConfig()
            obj = MemoryObject(0, sys_._tx_bytes, MemoryRequestType.KREAD, mem_config)
            req = MemoryRequest(memory_object=obj)

            with self.assertRaises(ValueError) as ctx:
                sys_.handler_mem_request([req])
            self.assertIn("bad_ctrl_param", str(ctx.exception))
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
