"""Strategy bots built on BaseSim (sim.py)."""
from .against import AgainstSim
from .combo import STRATEGIES, ComboSim
from .dual import DualSim
from .leader import LeaderSim
from .scalp import (
    ScalpSim,
    SCALP_ATR_MAX_USD,
    SCALP_DELTA_CAP_USD,
    SCALP_ENTRY_ELAPSED,
    SCALP_VELOCITY_MAX_USD_PER_MIN,
    SCALP_WARMUP_SECONDS,
    scalp_cancel_left_pct,
    scalp_delta_cap_usd,
    scalp_entry_elapsed_max,
    scalp_velocity_max_usd_per_min,
    scalp_warmup_seconds,
)

__all__ = [
    "AgainstSim", "ComboSim", "DualSim", "LeaderSim", "ScalpSim", "STRATEGIES",
    "SCALP_ATR_MAX_USD", "SCALP_DELTA_CAP_USD", "SCALP_ENTRY_ELAPSED",
    "SCALP_VELOCITY_MAX_USD_PER_MIN", "SCALP_WARMUP_SECONDS",
    "scalp_cancel_left_pct", "scalp_delta_cap_usd",
    "scalp_entry_elapsed_max", "scalp_velocity_max_usd_per_min",
    "scalp_warmup_seconds",
]
