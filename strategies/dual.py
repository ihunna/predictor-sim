"""Dual strategy: quiet mid-window entry, then deep-limit a cheap side or rest both.

Waits until ``enter_after_seconds`` into the window (and optional ``place_at`` seconds left).
If |BTC move from PTB| is at or above ``quiet_max_pct``, skips the window.
If one ask is at or below ``cheap_cents``, does **not** buy at the ask — rests a deep limit
at ``cheap_deep_cents`` on that side; when it fills, rests the other side at a complementary
price that keeps the same locked edge as both @ ``limit_cents``.
Otherwise rests ``limit_cents`` on both sides.
"""
from sim import BaseSim, Order


class DualSim(BaseSim):
    """Quiet-gated dual: deep-limit a cheap ask, else limit both sides."""

    strategy = "dual"
    start_mode = "dual"

    def __init__(self, *args, enter_after_seconds=60, quiet_max_pct=0.03, cheap_cents=30,
                 cheap_deep_cents=10, **kwargs):
        super().__init__(*args, **kwargs)
        self.enter_after_seconds = max(0, int(enter_after_seconds))
        self.quiet_max_pct = float(quiet_max_pct)
        self.cheap_cents = max(0, int(cheap_cents))
        self.cheap_deep_cents = max(0, int(cheap_deep_cents))

    def summarize(self):
        when = "at window start" if self.enter_after_seconds <= 0 else f"from {self.enter_after_seconds}s in"
        if self.place_at is not None:
            when += f" (and ≤{self.place_at}s left)"
        quiet = (
            f"|Δ|<{self.quiet_max_pct:.3f}% from PTB"
            if self.quiet_max_pct > 0 else "no quiet gate"
        )
        edge = max(0, 100 - 2 * self.limit_cents)
        if self.cheap_cents > 0 and self.cheap_deep_cents > 0:
            path = (
                f"ask≤{self.cheap_cents}¢ → deep limit @{self.cheap_deep_cents}¢ then other to +{edge}¢ lock; "
                f"else both @ {self.limit_cents}¢"
            )
        else:
            path = f"both limits @ {self.limit_cents}¢"
        skip = (
            f"skip after a last-window move of {self.max_prev_move_pct:.2f}% or more"
            if self.max_prev_move_pct > 0 else "no prev-move filter"
        )
        return (
            f"🎯 Dual: enter {when}, {quiet}; {path}; {self.dual_rules_text()}, {skip} "
            f"| capital ${self.capital:.2f} ({self.capital_source})"
        )

    def _gate_ready(self, state, remaining):
        """True when the entry clock allows a place-or-skip decision."""
        if self.place_at is not None and remaining > self.place_at:
            return False
        if self.enter_after_seconds > 0:
            elapsed = (state.window_end - state.window_start) - remaining
            if elapsed < self.enter_after_seconds:
                return False
        return True

    def _complement_cents(self, fill_cents):
        """Other-side limit so pair sum matches both @ limit (same locked edge)."""
        return min(99, max(self.limit_cents, 2 * self.limit_cents - fill_cents))

    def _first_orders(self, state, remaining, up_ask, down_ask):
        """After the entry gate: skip if not quiet; deep-limit cheap side or rest both."""
        if not self._gate_ready(state, remaining):
            return []

        if self.quiet_max_pct > 0:
            move = state.btc_move_pct
            if move is None:
                return []  # wait until BTC and PTB are known
            if abs(move) >= self.quiet_max_pct:
                state.trading = False
                state.action = "skip"
                state.action_reason = f"delta_{move:+.4f}pct"
                state.notes.append(f"delta_{move:+.4f}pct_not_quiet")
                state.notes.append("not_traded")
                return []

        cheap_thr = self.cheap_cents / 100.0
        if (self.cheap_cents > 0 and self.cheap_deep_cents > 0
                and min(up_ask, down_ask) <= cheap_thr + 1e-9):
            cheap_side = "Up" if up_ask <= down_ask else "Down"
            ask_cents = int(round(min(up_ask, down_ask) * 100))
            deep = self.cheap_deep_cents / 100.0
            state.action = "dual_deep"
            state.action_reason = (
                f"deep_{cheap_side.lower()}_{self.cheap_deep_cents}c_ask_{ask_cents}c"
            )
            state.notes.append(state.action_reason)
            state.dual_complement_placed = False
            return [Order(cheap_side, "main", deep, self.contracts, remaining)]

        state.action = "dual"
        state.action_reason = "rest_both"
        state.notes.append("rest_both")
        return [
            Order("Up", "main", self.limit, self.contracts, remaining),
            Order("Down", "main", self.limit, self.contracts, remaining),
        ]

    def _maybe_place_complement(self, state, remaining, asks):
        """After a deep cheap-side fill, rest the other side at a complementary limit."""
        if state.action != "dual_deep" or getattr(state, "dual_complement_placed", False):
            return
        mains = [o for o in state.orders if o.kind == "main"]
        filled = [o for o in mains if o.filled]
        if len(filled) != 1:
            return
        first = filled[0]
        other = "Down" if first.side == "Up" else "Up"
        if any(o.side == other for o in state.orders):
            state.dual_complement_placed = True
            return
        fill_cents = int(round(first.fill_price * 100))
        other_cents = self._complement_cents(fill_cents)
        order = Order(other, "main", other_cents / 100.0, self.contracts, remaining)
        state.orders.append(order)
        state.dual_complement_placed = True
        state.notes.append(f"complement_{other.lower()}_{other_cents}c_after_{fill_cents}c")
        order.check(remaining, asks[other])

    def _trade_tick(self, state, now, remaining, up_ask, down_ask):
        super()._trade_tick(state, now, remaining, up_ask, down_ask)
        if (not state.trading or state.decision_pending or state.cancelled_time_left is not None
                or state.stop_time_left is not None):
            return
        self._maybe_place_complement(state, remaining, {"Up": up_ask, "Down": down_ask})
