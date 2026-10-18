"""Leader strategy, built on BaseSim (sim.py), logged to the same trades format.

Trades only after a last-window move of ``min_prev_move_pct``% or more (a trending, non-dual window). Then it
watches from ``watch_from_pct`` to ``watch_to_pct`` of the window (1:00-2:00 into 5m) for the first side whose bid
(1 - the other side's ask) reaches ``leader_at_cents`` there (and not before), buys it at once at its ask (a taker
fill, skipped above ``leader_max_cents``) and holds to the end. No order on the other side, no hedge, cancel or
stop-loss. No leader: skipped."""
from sim import BaseSim, Order
from logs import format_time


class LeaderSim(BaseSim):
    strategy = "leader"
    start_mode = "leader"

    def __init__(self, *args, leader_at_cents=60, leader_max_cents=70, min_prev_move_pct=0.10, watch_from_pct=20.0,
                 watch_to_pct=40.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.leader_at_cents = leader_at_cents
        self.leader_max_cents = leader_max_cents
        self.min_prev_move_pct = min_prev_move_pct
        self.watch_from_pct = watch_from_pct
        self.watch_to_pct = watch_to_pct

    def watch_seconds(self):
        """(from, to) seconds into the window when a leader is looked for (60, 120 for 5m)."""
        length = self.duration * 60
        return length * self.watch_from_pct / 100.0, length * self.watch_to_pct / 100.0

    def prev_move_skip_reason(self, prev_move_pct):
        """Skip unless the last window moved ``min_prev_move_pct``% or more (0 = any move)."""
        if self.min_prev_move_pct <= 0:
            return None
        if prev_move_pct is None:
            return "prev_move_unknown"
        if abs(prev_move_pct) < self.min_prev_move_pct:
            return f"prev_move_{prev_move_pct:+.3f}pct_calm"
        return None

    def filter_text(self):
        if self.min_prev_move_pct <= 0:
            return "any last move"
        return f"only after a last-window move of {self.min_prev_move_pct:.2f}%+"

    def summarize(self):
        start, end = self.watch_seconds()
        savior = self.savior_summary_text()
        return (
            f"Leader: {self.filter_text()}; from {format_time(start)} to {format_time(end)} in, when a side's bid "
            f"first reaches {self.leader_at_cents}¢, buy {self.contracts} of it at its ask (≤{self.leader_max_cents}¢), "
            f"held to the end ({savior}); no leader -> skip | capital ${self.capital:.2f} ({self.capital_source})"
        )

    def order_cost(self):
        return self.contracts * self.leader_max_cents / 100

    def _trade_tick(self, state, now, remaining, up_ask, down_ask):
        elapsed = now - state.window_start
        bids = {
            "Up": round(1.0 - down_ask, 4) if down_ask > 0 else 0.0,
            "Down": round(1.0 - up_ask, 4) if up_ask > 0 else 0.0,
        }
        level = self.leader_at_cents / 100 - 1e-9
        watch_from, watch_to = self.watch_seconds()
        if elapsed < watch_from:
            state.reached_early.update(s for s, bid in bids.items() if bid >= level)
        if not state.trading or state.decision_pending:
            return
        asks = {"Up": up_ask, "Down": down_ask}
        if state.placed_time_left is None:
            if elapsed > watch_to:
                state.trading = False
                state.action, state.action_reason = "skip", "no_leader"
                state.notes.append(f"no_leader_by_{format_time(watch_to)}_in")
                state.notes.append("not_traded")
                return
            if elapsed < watch_from or up_ask <= 0 or down_ask <= 0:
                return
            leaders = [s for s, bid in bids.items() if bid >= level and s not in state.reached_early]
            if not leaders:
                return
            leader = max(leaders, key=bids.get)
            state.leader_side, state.leader_time_left = leader, remaining
            if asks[leader] > self.leader_max_cents / 100 + 1e-9:
                state.trading = False
                state.action, state.action_reason = "skip", f"leader_{leader.lower()}_ask_{round(asks[leader] * 100)}c"
                state.notes.append(f"leader_{leader.lower()}_ask_{round(asks[leader] * 100)}c_over_"
                                   f"{self.leader_max_cents}c")
                state.notes.append("not_traded")
                return
            state.action = "leader"
            state.action_reason = f"leader_{leader.lower()}_at_{format_time(elapsed)}_in"
            state.placed_time_left = remaining
            state.up_ask_at_place = up_ask
            state.down_ask_at_place = down_ask
            state.orders = [Order(leader, "leader", asks[leader], self.contracts, remaining)]
        for order in state.orders:
            order.check(remaining, asks[order.side])
        self._track_other_side(state, remaining, asks)
        self._check_stop_points(state, remaining, asks)
        if state.stop_time_left is None and state.tp_side is None:
            self._position_exits(state, now, remaining, up_ask, down_ask)

    def resolve_decision(self, state, skip_reason=None, mode="leader"):
        super().resolve_decision(state, skip_reason, mode="leader")
        if skip_reason:
            state.action, state.action_reason = "skip", skip_reason

    def limit_text(self):
        return f"ask<={self.leader_max_cents}"

    def status_label(self):
        return "Leader"

    def waiting_text(self):
        start, end = self.watch_seconds()
        return f"waiting for a leader ({format_time(start)}-{format_time(end)} in)"

    def orders_text(self, state):
        return " ".join(f"{o.side} {o.contracts} @ {round(o.fill_price * 100)}¢" for o in state.orders if o.filled)
