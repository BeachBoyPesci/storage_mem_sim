"""Discrete-event simulator — event types and simulation adapter.

Import direction: ``des → memory_pool`` only.
"""

from .event import Event
from .result import SimulationResult
from .simulator import SimpleSimulator

__all__ = [
    "Event",
    "SimpleSimulator",
    "SimulationResult",
]
