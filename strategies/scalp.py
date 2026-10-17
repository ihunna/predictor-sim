"""Cheap-side hold scalper: buy a dumped ask and hold to settlement.

Merged updown/trade logs: WR on the cheap side beats the ask → edge is in holding.
Defaults from that data (entry ≤20¢, no early exit). Optional TP / time-exit / confirmed
stop remain available via flags but are off by default — early exits cut EV vs hold.

Bid is inferred as ``1 − other_ask`` (same convention as against take-profit).
"""

from logs import format_time
from sim import BaseSim, Order


class ScalpSim(BaseSim):
    """Single-side: buy ask≤entry, hold to settle (optional TP / time / confirmed stop)."""

    strategy = "scalp"
    start_mode = "against"

    def __init__(self, *args, entry_cents=20, target_cents=0, exit_left_seconds=0,
                 stop_cents=0, stop_confirm_seconds=5, **kwargs):
        kwargs.setdefault("cancel_left_pct", 0.0)
        kwargs.setdefault("stop_loss_seconds", 0)
        super().__init__(*args, **kwargs)
        self.entry_cents = max(1, int(entry_cents))
        # 0 = hold to settle (no take-profit)
        self.target_cents = max(0, int(target_cents))
        if self.target_cents > 0 and self.target_cents <= self.entry_cents:
            self.target_cents = self.entry_cents + 1
        self.exit_left_seconds = max(0, int(exit_left_seconds))
        # 0 = no hard stop (default — data: stops cut winners before maturity)
        self.stop_cents = max(0, int(stop_cents if stop_cents is not None else 0))
        self.stop_confirm_seconds = max(0, int(stop_confirm_seconds))

    def summarize(self):
        if self.target_cents > 0:
            tp = f"TP bid≥{self.target_cents}¢"
        else:
            tp = "hold to settle"
        stop = (
            f"hard stop ≤{self.stop_cents}¢ for {self.stop_confirm_seconds}s"
            if self.stop_cents > 0 else "no hard stop"
        )
        time_x = (
            f"flatten at {format_time(self.exit_left_seconds)} left"
            if self.exit_left_seconds > 0 else "no time exit"
        )
        return (
            f"🎯 Scalp: buy ask≤{self.entry_cents}¢ ×{self.contracts}, "
            f"{tp}, {time_x}, {stop} "
            f"| capital ${self.capital:.2f} ({self.capital_source})"
        )

    @staticmethod
    def _bid(side, up_ask, down_ask):
        other = down_ask if side == "Up" else up_ask
        if other <= 0:
            return 0.0
        return round(1.0 - other, 4)

    def _first_orders(self, state, remaining, up_ask, down_ask):
        if self.place_at is not None and remaining > self.place_at:
            return []
        if self.exit_left_seconds > 0 and remaining <= self.exit_left_seconds:
            return []

        entry = self.entry_cents / 100.0
        candidates = []
        if 0 < up_ask <= entry + 1e-9:
            candidates.append(("Up", up_ask))
        if 0 < down_ask <= entry + 1e-9:
            candidates.append(("Down", down_ask))
        if not candidates:
            return []

        side, ask = min(candidates, key=lambda x: x[1])
        state.mode = "against"
        state.bet_side = side
        state.take_profit = self.target_cents / 100.0 if self.target_cents > 0 else 0.0
        state.action = "scalp"
        if self.target_cents > 0:
            state.action_reason = f"buy_{side.lower()}_{int(round(ask * 100))}c_tp_{self.target_cents}c"
        else:
            state.action_reason = f"buy_{side.lower()}_{int(round(ask * 100))}c_hold"
        state.notes.append(state.action_reason)
        state.scalp_stop_since = None
        return [Order(side, "main", ask, self.contracts, remaining)]

    def _maybe_take_profit(self, state, remaining, up_ask, down_ask):
        if not state.take_profit or state.tp_side or not state.bet_side:
            return
        if state.stop_time_left is not None:
            return
        side = state.bet_side
        held = state.held(side)
        bid = self._bid(side, up_ask, down_ask)
        if held <= 0 or bid < state.take_profit - 1e-9:
            return
        state.tp_side, state.tp_contracts, state.tp_time_left = side, held, remaining
        state.notes.append(
            f"tp_sold_{side.lower()}_{held}@{round(state.take_profit * 100)}_at_"
            f"{format_time(remaining)}_left"
        )

    def _maybe_time_exit(self, state, remaining, up_ask, down_ask):
        if self.exit_left_seconds <= 0 or remaining > self.exit_left_seconds:
            return
        if state.tp_side or state.stop_time_left is not None or not state.bet_side:
            return
        side = state.bet_side
        held = state.held(side)
        bid = self._bid(side, up_ask, down_ask)
        if held <= 0 or bid <= 0:
            return
        state.stop_side, state.stop_contracts, state.stop_price = side, held, bid
        state.stop_time_left = remaining
        state.notes.append(
            f"time_sold_{side.lower()}_{held}@{round(bid * 100)}_at_{format_time(remaining)}_left"
        )

    def _maybe_hard_stop(self, state, now, remaining, up_ask, down_ask):
        """Optional wide stop only after bid stays ≤ stop for stop_confirm_seconds."""
        if self.stop_cents <= 0 or not state.bet_side:
            return
        if state.tp_side or state.stop_time_left is not None:
            return
        side = state.bet_side
        held = state.held(side)
        bid = self._bid(side, up_ask, down_ask)
        if held <= 0 or bid <= 0:
            state.scalp_stop_since = None
            return

        stop = self.stop_cents / 100.0
        if bid > stop + 1e-9:
            state.scalp_stop_since = None
            return

        since = getattr(state, "scalp_stop_since", None)
        if since is None:
            state.scalp_stop_since = now
            return
        if self.stop_confirm_seconds > 0 and (now - since) < self.stop_confirm_seconds:
            return

        state.stop_side, state.stop_contracts, state.stop_price = side, held, bid
        state.stop_time_left = remaining
        state.notes.append(
            f"stop_sold_{side.lower()}_{held}@{round(bid * 100)}_held_"
            f"{self.stop_confirm_seconds}s_at_{format_time(remaining)}_left"
        )

    def _trade_tick(self, state, now, remaining, up_ask, down_ask):
        super()._trade_tick(state, now, remaining, up_ask, down_ask)
        if (not state.trading or state.decision_pending or state.cancelled_time_left is not None
                or state.stop_time_left is not None or state.tp_side):
            return
        self._maybe_take_profit(state, remaining, up_ask, down_ask)
        if state.tp_side:
            return
        self._maybe_hard_stop(state, now, remaining, up_ask, down_ask)
        if state.stop_time_left is not None:
            return
        self._maybe_time_exit(state, remaining, up_ask, down_ask)

    def orders_text(self, state):
        text = super().orders_text(state)
        if state.tp_side:
            text += f" · TP {state.tp_contracts} @ {round(state.take_profit * 100)}¢"
        elif state.take_profit:
            text += f" · TP {round(state.take_profit * 100)}¢"
        elif state.bet_side and state.held(state.bet_side) > 0:
            text += " · hold"
        return text
