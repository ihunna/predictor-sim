"""Cheap-side scalper: buy a dumped ask, take profit on bid, else full loss at settle.

Same optional structure gate as dual (default: off — trade any structure; log struct/RSI).
Default path: buy ≤30¢, TP bid ≥60¢. Optional ``target_cents=0`` / time-exit / confirmed stop
via flags. Bid is inferred as ``1 − other_ask`` (same convention as against take-profit).
"""

from logs import format_time
from sim import BaseSim, Order


class ScalpSim(BaseSim):
    """Single-side: buy ask≤entry, sell bid≥TP (default 30→60); miss holds and loses."""

    strategy = "scalp"
    start_mode = "against"

    def __init__(self, *args, entry_cents=30, target_cents=60, exit_left_seconds=0,
                 stop_cents=0, stop_confirm_seconds=5, require_structure="", **kwargs):
        # Block new entries / cancel unfilled once cancel_left_pct of the window is left
        # (default 67% ≈ first third only; 60m scalp: late ≤30¢ fills rarely reach TP60).
        kwargs.setdefault("cancel_left_pct", 67.0)
        kwargs.setdefault("stop_loss_seconds", 0)
        kwargs.setdefault("max_prev_move_pct", 0.0)
        super().__init__(*args, **kwargs)
        self.entry_cents = max(1, int(entry_cents))
        # 0 = hold to settle (no take-profit); default 60 from 5m replay
        self.target_cents = max(0, int(target_cents))
        if self.target_cents > 0 and self.target_cents <= self.entry_cents:
            self.target_cents = self.entry_cents + 1
        self.exit_left_seconds = max(0, int(exit_left_seconds))
        # 0 = no hard stop
        self.stop_cents = max(0, int(stop_cents if stop_cents is not None else 0))
        self.stop_confirm_seconds = max(0, int(stop_confirm_seconds))
        self.require_structure = (require_structure or "").strip().lower()

    def summarize(self):
        if self.target_cents > 0:
            tp = f"TP bid≥{self.target_cents}¢ (miss=−entry)"
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
        gate = (
            f"structure={self.require_structure}"
            if self.require_structure else "any structure"
        )
        cancel = (
            f"cancel no-fill @{format_time(self.cancel_left_seconds())} left"
            if self.cancel_left_pct > 0 else "never cancel"
        )
        savior = (
            f"savior @{format_time(self.savior_left_seconds())} left"
            if self.savior_left_pct > 0 else "no savior"
        )
        return (
            f"🎯 Scalp: buy ask≤{self.entry_cents}¢ ×{self.contracts} when {gate}, "
            f"{tp}, {time_x}, {stop}, {cancel}, {savior} "
            f"| capital ${self.capital:.2f} ({self.capital_source})"
        )

    @staticmethod
    def _bid(side, up_ask, down_ask):
        other = down_ask if side == "Up" else up_ask
        if other <= 0:
            return 0.0
        return round(1.0 - other, 4)

    def _structure_ok(self, state):
        """Same as dual: always refresh for log/UI; gate only when require_structure is set."""
        avg, vs_avg, rsi, structure = self.refresh_structure(state.window_start)
        state.avg_move_pct = avg
        state.move_vs_avg = vs_avg
        state.rsi = rsi
        state.structure = structure or ""
        if not self.require_structure:
            return True
        return (structure or "").lower() == self.require_structure

    def _first_orders(self, state, remaining, up_ask, down_ask):
        if self.place_at is not None and remaining > self.place_at:
            return []
        if self.exit_left_seconds > 0 and remaining <= self.exit_left_seconds:
            return []
        # Same duration-relative cutoff as dual cancel: no new buys once that much is left.
        if self.cancel_left_pct > 0 and remaining <= self.cancel_left_seconds():
            return []
        if not self._structure_ok(state):
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
        struct = state.structure or "any"
        if self.target_cents > 0:
            state.action_reason = (
                f"buy_{side.lower()}_{int(round(ask * 100))}c_tp_{self.target_cents}c"
                f"_structure_{struct}"
            )
        else:
            state.action_reason = (
                f"buy_{side.lower()}_{int(round(ask * 100))}c_hold_structure_{struct}"
            )
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

    def _position_exits(self, state, now, remaining, up_ask, down_ask):
        """TP / hard stop / time exit first; shared savior only if still holding."""
        self._maybe_take_profit(state, remaining, up_ask, down_ask)
        if state.tp_side:
            return
        self._maybe_hard_stop(state, now, remaining, up_ask, down_ask)
        if state.stop_time_left is not None:
            return
        self._maybe_time_exit(state, remaining, up_ask, down_ask)
        if state.stop_time_left is not None:
            return
        super()._position_exits(state, now, remaining, up_ask, down_ask)

    def orders_text(self, state):
        text = super().orders_text(state)
        if state.tp_side:
            text += f" · TP {state.tp_contracts} @ {round(state.take_profit * 100)}¢"
        elif state.take_profit:
            text += f" · TP {round(state.take_profit * 100)}¢"
        elif state.savior_cut_cents is not None and state.stop_time_left is None:
            text += f" · savior cut {state.savior_cut_cents}¢"
        elif state.bet_side and state.held(state.bet_side) > 0:
            text += " · hold"
        return text
