"""Dual: min-move / max-action → buy cheap + rest the other at a matching lock price.

Watches from ``enter_after_seconds`` (0 = from open) until late in the window.
When |BTC Δ from PTB| ≤ ``quiet_max_pct`` and one ask is ≤ ``cheap_cents``:

* lift ``contracts`` on the cheap side at its ask
* rest the same size on the other side at ``pair_cents − cheap_ask`` (locked edge if both fill)

Equal contracts, unequal prices. If only the cheap side fills, flatten it by
``strand_exit_seconds`` left (sell at bid) so unfinished cheap legs do not sit to resolution.

If the book never triggers that signal, optionally rests both @ ``limit_cents`` when still quiet
and both asks are above ``cheap_cents`` (balanced path). ``run.py`` scales quiet % by duration.
"""

from logs import format_time
from sim import BaseSim, Order


class DualSim(BaseSim):
    """Event dual: cheap lift + equal-size complement, or balanced both @ limit."""

    strategy = "dual"
    start_mode = "dual"

    def __init__(self, *args, enter_after_seconds=0, quiet_max_pct=0.01, cheap_cents=30,
                 pair_cents=80, strand_exit_seconds=0, allow_balanced=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.enter_after_seconds = max(0, int(enter_after_seconds))
        self.quiet_max_pct = float(quiet_max_pct)
        self.cheap_cents = max(0, int(cheap_cents))
        self.pair_cents = max(2, min(198, int(pair_cents)))
        self.strand_exit_seconds = max(0, int(strand_exit_seconds))
        self.allow_balanced = bool(allow_balanced)

    def summarize(self):
        when = "from open" if self.enter_after_seconds <= 0 else f"from {self.enter_after_seconds}s in"
        quiet = (
            f"|Δ|≤{self.quiet_max_pct:.3f}% + ask≤{self.cheap_cents}¢"
            if self.quiet_max_pct > 0 else f"ask≤{self.cheap_cents}¢ (no Δ gate)"
        )
        lock = f"lift cheap ×{self.contracts}, other @{self.pair_cents}¢−ask (equal size)"
        bal = (
            f"; else both @{self.limit_cents}¢ if still quiet"
            if self.allow_balanced else "; else skip"
        )
        strand = (
            f"flatten stranded cheap at {format_time(self.strand_exit_seconds)} left"
            if self.strand_exit_seconds > 0 else "hold stranded cheap"
        )
        skip = (
            f"skip after a last-window move of {self.max_prev_move_pct:.2f}% or more"
            if self.max_prev_move_pct > 0 else "no prev-move filter"
        )
        return (
            f"🎯 Dual: {when}, {quiet} → {lock}{bal}; {strand}; {skip} "
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

    def _complement_cents(self, cheap_ask_cents):
        return min(99, max(1, self.pair_cents - cheap_ask_cents))

    def _first_orders(self, state, remaining, up_ask, down_ask):
        if not self._gate_ready(state, remaining):
            return []

        delta_ok = self._delta_ok(state)
        if delta_ok is None:
            return []

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

        if not self.allow_balanced or not delta_ok:
            return []

        state.action = "dual"
        state.action_reason = "rest_both"
        state.notes.append("rest_both")
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
