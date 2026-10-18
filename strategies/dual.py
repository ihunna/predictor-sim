"""Dual: rest both sides at the limit, only when BTC structure is mixed.

No quiet-Δ, enter-after, cheap-skew, or prev-move gate by default. Structure comes from
the shared ``PriceStructure`` (official window moves; lookbacks scale with duration).
Unknown / up / down → skip. Unfilled both-sides cancel at ``cancel_left_pct`` of the window
(duration-relative; default 67% left ≈ only the first third of the window).
"""

from logs import format_time
from sim import BaseSim, Order


class DualSim(BaseSim):
    """Both limits at ``limit_cents`` when structure == mixed."""

    strategy = "dual"
    start_mode = "dual"

    def __init__(self, *args, require_structure="mixed", enter_after_seconds=0, quiet_max_pct=0.0,
                 cheap_cents=0, pair_cents=70, strand_exit_seconds=0, allow_balanced=True, **kwargs):
        # Cancel no-fill is duration-relative via cancel_left_pct (default 67% left ≈ first third).
        kwargs.setdefault("cancel_left_pct", 67.0)
        kwargs.setdefault("stop_loss_seconds", 0)
        kwargs.setdefault("max_prev_move_pct", 0.0)
        super().__init__(*args, **kwargs)
        self.require_structure = (require_structure or "").strip().lower()
        self.enter_after_seconds = max(0, int(enter_after_seconds))
        self.quiet_max_pct = float(quiet_max_pct)
        self.cheap_cents = max(0, int(cheap_cents))
        self.pair_cents = max(2, min(198, int(pair_cents)))
        self.strand_exit_seconds = max(0, int(strand_exit_seconds))
        self.allow_balanced = bool(allow_balanced)

    def summarize(self):
        gate = (
            f"structure={self.require_structure}"
            if self.require_structure else "any structure"
        )
        cancel = (
            f"cancel no-fill @{format_time(self.cancel_left_seconds())} left"
            if self.cancel_left_pct > 0 else "never cancel"
        )
        return (
            f"🎯 Dual: both @{self.limit_cents}¢ ×{self.contracts} when {gate} "
            f"({cancel}; no Δ / enter-after / skew) "
            f"| capital ${self.capital:.2f} ({self.capital_source})"
        )

    def _elapsed(self, state, remaining):
        return (state.window_end - state.window_start) - remaining

    def _gate_ready(self, state, remaining):
        if self.place_at is not None and remaining > self.place_at:
            return False
        if self.enter_after_seconds > 0 and self._elapsed(state, remaining) < self.enter_after_seconds:
            return False
        return True

    def _delta_ok(self, state):
        if self.quiet_max_pct <= 0:
            return True
        move = state.btc_move_pct
        if move is None:
            return None
        return abs(move) <= self.quiet_max_pct + 1e-12

    def _structure_ok(self, state):
        """Refresh trend from prior windows; require mixed (or whatever require_structure is)."""
        if not self.require_structure:
            return True
        avg, vs_avg, rsi, structure = self.refresh_structure(state.window_start)
        state.avg_move_pct = avg
        state.move_vs_avg = vs_avg
        state.rsi = rsi
        state.structure = structure or ""
        return (structure or "").lower() == self.require_structure

    def _complement_cents(self, cheap_ask_cents):
        return min(99, max(1, self.pair_cents - cheap_ask_cents))

    def _first_orders(self, state, remaining, up_ask, down_ask):
        if not self._gate_ready(state, remaining):
            return []
        if not self._structure_ok(state):
            return []

        delta_ok = self._delta_ok(state)
        if delta_ok is None:
            return []

        # Optional legacy skew path (only if cheap_cents > 0).
        cheap_thr = self.cheap_cents / 100.0
        min_ask = min(up_ask, down_ask)
        skewed = self.cheap_cents > 0 and min_ask <= cheap_thr + 1e-9

        if skewed and delta_ok:
            cheap_side = "Up" if up_ask <= down_ask else "Down"
            other = "Down" if cheap_side == "Up" else "Up"
            cheap_ask = up_ask if cheap_side == "Up" else down_ask
            ask_cents = int(round(cheap_ask * 100))
            other_cents = self._complement_cents(ask_cents)
            state.action = "dual_skew"
            state.action_reason = f"lift_{cheap_side.lower()}_{ask_cents}c_other_{other_cents}c"
            state.notes.append(state.action_reason)
            state.mode = "dual"
            return [
                Order(cheap_side, "main", cheap_ask, self.contracts, remaining),
                Order(other, "main", other_cents / 100.0, self.contracts, remaining),
            ]

        if skewed and not delta_ok:
            return []

        if self.cheap_cents > 0 and not self.allow_balanced:
            return []
        if not delta_ok:
            return []

        state.action = "dual"
        state.action_reason = f"rest_both_structure_{state.structure or 'any'}"
        state.notes.append(state.action_reason)
        return [
            Order("Up", "main", self.limit, self.contracts, remaining),
            Order("Down", "main", self.limit, self.contracts, remaining),
        ]

    def _maybe_strand_exit(self, state, remaining, up_ask, down_ask):
        if self.strand_exit_seconds <= 0 or remaining > self.strand_exit_seconds:
            return
        if state.stop_time_left is not None or state.tp_side:
            return
        if state.fill_type not in ("only_up", "only_down"):
            return
        side = "Up" if state.fill_type == "only_up" else "Down"
        held = state.held(side)
        if held <= 0:
            return
        other_ask = down_ask if side == "Up" else up_ask
        if other_ask <= 0:
            return
        bid = round(1.0 - other_ask, 4)
        if bid <= 0:
            return
        state.stop_side, state.stop_contracts, state.stop_price = side, held, bid
        state.stop_time_left = remaining
        state.notes.append(
            f"strand_sold_{side.lower()}_{held}@{round(bid * 100)}_at_{format_time(remaining)}_left"
        )

    def _trade_tick(self, state, now, remaining, up_ask, down_ask):
        super()._trade_tick(state, now, remaining, up_ask, down_ask)
        if (not state.trading or state.decision_pending or state.cancelled_time_left is not None
                or state.stop_time_left is not None):
            return
        self._maybe_strand_exit(state, remaining, up_ask, down_ask)
