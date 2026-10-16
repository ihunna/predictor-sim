"""Dual strategy bot: limit buy on both Up and Down every window.

Built on ``BaseSim`` (sim.py), same as leader and combo/against.
"""
from sim import BaseSim

class DualSim(BaseSim):
    """Dual strategy: limit buy on both Up and Down every window."""

    strategy = "dual"
    start_mode = "dual"
