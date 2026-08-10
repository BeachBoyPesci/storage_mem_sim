"""Discrete-event simulator — event types and simulation adapter.

Import direction: ``des → memory_pool`` only.
"""

from ..memory_pool.event import Event, EventKind
from .result import SimulationResult
from .simulator import SimpleSimulator

__all__ = [
    "Event",
    "EventKind",
    "SimpleSimulator",
    "SimulationResult",
]


