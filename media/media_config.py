"""Media configuration.

Defines configuration parameters for the media simulation layer.
"""

from dataclasses import dataclass, field
from .media_backend import MediaSystemBackend


@dataclass
class MediaConfig:
    """Configuration for a media simulation backend.

    Attributes:
        media_type: Which backend to use (ANALYTIC, RAMULATOR, MQSIM).
        config_path: Path to the backend-specific configuration file
                     (YAML for Ramulator, XML for MQSim, unused for Analytic).
        capacity: Device capacity in GB.
        bandwidth: Peak bandwidth in GB/s (used by Analytic backend).
        io_frequency: I/O clock frequency in MHz (used by Ramulator to convert
                      cycles -> time: scale_factor = 1.0 / (io_frequency * 10^6)).
        granularity: Access granularity in bytes. For Ramulator, auto-derived
                     from DRAM spec; fallback for other backends.

        # MQSim-specific
        ssd_config_path: Path to MQSim ssdconfig.xml.
        workload_config_path: Path to MQSim workload.xml.
        request_size_bytes: Typical request size in bytes.
                            Large -> bandwidth-bound, small -> IOPS-bound.
    """
    media_type: MediaSystemBackend = MediaSystemBackend.ANALYTIC
    config_path: str = ""
    capacity: float = 0.0
    bandwidth: float = 0.0
    io_frequency: float = 0.0
    granularity: int = 64

    # Transport link bandwidth (Analytic only). 0.0 = not configured =
    # no transport bottleneck; the effective peak is
    # min(bandwidth, transport_bandwidth).
    transport_bandwidth: float = 0.0

    # MQSim-specific
    ssd_config_path: str = ""
    workload_config_path: str = ""
    request_size_bytes: int = 131072
    merge_contiguous: bool = True

    def __post_init__(self):
        if self.transport_bandwidth < 0:
            raise ValueError(
                f"transport_bandwidth must be >= 0, got "
                f"{self.transport_bandwidth}"
            )

    @property
    def scale_factor(self) -> float:
        """Cycles -> seconds: 1.0 / (io_frequency * 10^6)."""
        if self.io_frequency > 0:
            return 1.0 / (self.io_frequency * 1e6)
        return 1.0
