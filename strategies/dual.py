"""Dual strategy: quiet mid-window entry, then lift the cheap side or rest both limits.

Waits until ``enter_after_seconds`` into the window (and optional ``place_at`` seconds left).
If |BTC move from PTB| is at or above ``quiet_max_pct``, skips the window.
If one ask is at or below ``cheap_cents``, buys that side at the ask (taker) and rests the
other side at ``limit_cents``. Otherwise rests ``limit_cents`` on both sides.
"""
from sim import BaseSim, Order


class DualSim(BaseSim):
    """Quiet-gated dual: lift a really-cheap ask, else limit both sides."""

    strategy = "dual"
    start_mode = "dual"

    def __init__(self, *args, enter_after_seconds=60, quiet_max_pct=0.03, cheap_cents=30, **kwargs):
        super().__init__(*args, **kwargs)
        self.enter_after_seconds = max(0, int(enter_after_seconds))
        self.quiet_max_pct = float(quiet_max_pct)
        self.cheap_cents = max(0, int(cheap_cents))

    def summarize(self):
        when = "at window start" if self.enter_after_seconds <= 0 else f"from {self.enter_after_seconds}s in"
        if self.place_at is not None:
            when += f" (and ≤{self.place_at}s left)"
        quiet = (
            f"|Δ|<{self.quiet_max_pct:.3f}% from PTB"
            if self.quiet_max_pct > 0 else "no quiet gate"
        )
        edge = max(0, 100 - 2 * self.limit_cents)
        lift = (
            f"lift ask≤{self.cheap_cents}¢, other limit complements to +{edge}¢ lock; "
            f"else both @ {self.limit_cents}¢"
            if self.cheap_cents > 0 else f"both limits @ {self.limit_cents}¢"
        )
        skip = (
            f"skip after a last-window move of {self.max_prev_move_pct:.2f}% or more"
            if self.max_prev_move_pct > 0 else "no prev-move filter"
        )
        return (
            f"🎯 Dual: enter {when}, {quiet}; {lift}; {self.dual_rules_text()}, {skip} "
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

    def _first_orders(self, state, remaining, up_ask, down_ask):
        """After the entry gate: skip if not quiet; lift cheap ask or rest both limits."""
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
        if self.cheap_cents > 0 and min(up_ask, down_ask) <= cheap_thr + 1e-9:
            if up_ask <= down_ask:
                cheap_side, cheap_ask, other = "Up", up_ask, "Down"
            else:
                cheap_side, cheap_ask, other = "Down", down_ask, "Up"
            cheap_cents = int(round(cheap_ask * 100))
            # Same locked edge as resting both @ limit: other = (2*limit) - cheap
            other_cents = min(99, max(self.limit_cents, 2 * self.limit_cents - cheap_cents))
            other_limit = other_cents / 100.0
            state.action = "dual_lift"
            state.action_reason = f"lift_{cheap_side.lower()}_{cheap_cents}c_other_{other_cents}c"
            state.notes.append(state.action_reason)
            return [
                Order(cheap_side, "main", cheap_ask, self.contracts, remaining),
                Order(other, "main", other_limit, self.contracts, remaining),
            ]

        state.action = "dual"
        state.action_reason = "rest_both"
        state.notes.append("rest_both")
        return [
            Order("Up", "main", self.limit, self.contracts, remaining),
            Order("Down", "main", self.limit, self.contracts, remaining),
        ]
