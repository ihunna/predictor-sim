"""Cheap-side scalper: buy a dumped ask, take profit on bid, else full loss at settle.

Same optional structure gate as dual (default: off — trade any structure; log struct/RSI).
Optional ``rsi_zone=(lo, hi)`` skips entries outside that RSI band (off by default).
Default path (2026-10-21): split entry — half the size at ask ≤30¢ (take-profit at
bid ≥60¢), the other half only if the ask falls to ≤20¢ (take-profit at bid ≥40¢).
Optional ``delta_cap_usd`` skips entries when this window's BTC move against the
bought side already exceeds the cap at entry (per-duration caps via run.py).
Optional ``target_cents=0`` / time-exit / confirmed stop via flags. Bid is inferred as
``1 − other_ask`` (same convention as against take-profit).
"""

from logs import format_time
from sim import BaseSim, Order


class ScalpSim(BaseSim):
    """Single-side: buy ask≤entry, ladder TP 40/60 by default; miss holds and loses."""

    strategy = "scalp"
    start_mode = "against"

    def __init__(self, *args, entry_cents=30, target_cents=60, ladder_first_cents=0,
                 split_entry_cents=20, split_target_cents=40, delta_cap_usd=0.0,
                 exit_left_seconds=0,
                 stop_cents=0, stop_confirm_seconds=5, require_structure="",
                 rsi_zone=None, **kwargs):
        # Block new entries / cancel unfilled once cancel_left_pct of the window is left
        # (default 67% ≈ first third only; 60m scalp: late ≤30¢ fills rarely reach TP60).
        kwargs.setdefault("cancel_left_pct", 67.0)
        kwargs.setdefault("stop_loss_seconds", 0)
        kwargs.setdefault("max_prev_move_pct", 0.0)
        super().__init__(*args, **kwargs)
        self.entry_cents = max(1, int(entry_cents))
        # 0 = hold to settle (no take-profit); final TP default 60 from replay
        self.target_cents = max(0, int(target_cents))
        if self.target_cents > 0 and self.target_cents <= self.entry_cents:
            self.target_cents = self.entry_cents + 1
        # First ladder tranche; 0 or outside (entry, target) disables the ladder (all-in target).
        self.ladder_first_cents = max(0, int(ladder_first_cents or 0))
        if not (self.entry_cents < self.ladder_first_cents < self.target_cents):
            self.ladder_first_cents = 0
        # Split entry: second half only at/below split_entry_cents, exited at
        # split_target_cents. 0 disables (single all-in entry at entry_cents).
        self.split_entry_cents = max(0, int(split_entry_cents or 0))
        if not (0 < self.split_entry_cents < self.entry_cents):
            self.split_entry_cents = 0
        self.split_target_cents = max(0, int(split_target_cents or 0))
        if self.split_entry_cents and not (
            self.split_entry_cents < self.split_target_cents <= max(self.target_cents, self.split_entry_cents + 1)
        ):
            self.split_target_cents = self.entry_cents + 10
        # Skip entries when this window's move against the bought side exceeds
        # this many USD at entry (0 = off). Set per duration by run.py.
        self.delta_cap_usd = max(0.0, float(delta_cap_usd or 0.0))
        self.exit_left_seconds = max(0, int(exit_left_seconds))
        # 0 = no hard stop
        self.stop_cents = max(0, int(stop_cents if stop_cents is not None else 0))
        self.stop_confirm_seconds = max(0, int(stop_confirm_seconds))
        self.require_structure = (require_structure or "").strip().lower()
        # None = no RSI gate; else (lo, hi) inclusive, 0–100
        self.rsi_zone = rsi_zone

    @property
    def ladder_active(self):
        return self.ladder_first_cents > 0 and self.target_cents > 0

    @property
    def split_active(self):
        return self.split_entry_cents > 0 and self.target_cents > 0 and self.contracts >= 2

    def tp_text(self):
        if self.target_cents <= 0:
            return "hold"
        if self.split_active:
            return (f"TP {self.target_cents}¢ (1st ½) / {self.split_target_cents}¢ "
                    f"(2nd ½ @{self.split_entry_cents}¢)")
        if self.ladder_active:
            return f"TP ladder {self.ladder_first_cents}/{self.target_cents}¢"
        return f"TP {self.target_cents}¢"

    def summarize(self):
        if self.split_active:
            tp = (f"split entry: ½ @{self.entry_cents}¢ → TP bid≥{self.target_cents}¢, "
                  f"½ @{self.split_entry_cents}¢ → TP bid≥{self.split_target_cents}¢ "
                  f"(miss=−entry)")
        elif self.ladder_active:
            tp = (f"TP ladder bid≥{self.ladder_first_cents}¢ (½) → ≥{self.target_cents}¢ (½) "
                  f"(miss=−entry)")
        elif self.target_cents > 0:
            tp = f"TP bid≥{self.target_cents}¢ (miss=−entry)"
        else:
            tp = "hold to settle"
        if self.delta_cap_usd > 0:
            tp += f", skip if adverse Δ>${self.delta_cap_usd:.0f} at entry"
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
        if self.rsi_zone:
            lo, hi = self.rsi_zone
            rsi_gate = f"RSI {lo:g}–{hi:g}"
        else:
            rsi_gate = "any RSI"
        cancel = (
            f"cancel no-fill @{format_time(self.cancel_left_seconds())} left"
            if self.cancel_left_pct > 0 else "never cancel"
        )
        savior = self.savior_summary_text()
        return (
            f"Scalp: buy ask≤{self.entry_cents}¢ x{self.contracts} when {gate}, {rsi_gate}, "
            f"{tp}, {time_x}, {stop}, {cancel}, {savior} "
            f"| capital ${self.capital:.2f} ({self.capital_source})"
        )

    @staticmethod
    def _bid(side, up_ask, down_ask):
        other = down_ask if side == "Up" else up_ask
        if other <= 0:
            return 0.0
        return round(1.0 - other, 4)

    def _entry_ok(self, state):
        """Refresh struct/RSI for log/UI; gate on require_structure and optional rsi_zone."""
        avg, vs_avg, rsi, structure = self.refresh_structure(state.window_start)
        state.avg_move_pct = avg
        state.move_vs_avg = vs_avg
        state.rsi = rsi
        state.structure = structure or ""
        if self.require_structure and (structure or "").lower() != self.require_structure:
            return False
        if self.rsi_zone is not None:
            lo, hi = self.rsi_zone
            if rsi is None or not (lo - 1e-9 <= rsi <= hi + 1e-9):
                return False
        return True

    def _first_orders(self, state, remaining, up_ask, down_ask):
        if self.place_at is not None and remaining > self.place_at:
            return []
        if self.exit_left_seconds > 0 and remaining <= self.exit_left_seconds:
            return []
        # Same duration-relative cutoff as dual cancel: no new buys once that much is left.
        if self.cancel_left_pct > 0 and remaining <= self.cancel_left_seconds():
            return []
        if not self._entry_ok(state):
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

        # Adverse-delta cap: this window's BTC move (USD) against the side we
        # would buy. Too much ground already lost in this window → no entry.
        if self.delta_cap_usd > 0:
            adverse = self._adverse_usd(state, side)
            if adverse is None or adverse > self.delta_cap_usd + 1e-9:
                return []

        state.mode = "against"
        state.bet_side = side
        state.take_profit = self.target_cents / 100.0 if self.target_cents > 0 else 0.0
        state.action = "scalp"
        struct = state.structure or "any"
        rsi_bit = f"_rsi_{state.rsi:.0f}" if state.rsi is not None else ""
        if self.ladder_active:
            state.action_reason = (
                f"buy_{side.lower()}_{int(round(ask * 100))}c_tp_ladder_"
                f"{self.ladder_first_cents}_{self.target_cents}c"
                f"_structure_{struct}{rsi_bit}"
            )
        elif self.target_cents > 0:
            state.action_reason = (
                f"buy_{side.lower()}_{int(round(ask * 100))}c_tp_{self.target_cents}c"
                f"_structure_{struct}{rsi_bit}"
            )
        else:
            state.action_reason = (
                f"buy_{side.lower()}_{int(round(ask * 100))}c_hold_structure_{struct}{rsi_bit}"
            )
        state.notes.append(state.action_reason)
        state.scalp_stop_since = None

        if not self.split_active:
            return [Order(side, "main", ask, self.contracts, remaining)]
        first_size = self.contracts // 2
        second_size = self.contracts - first_size
        if ask <= self.split_entry_cents / 100.0 + 1e-9:
            # Already at/below the deep level: both halves fill now at the ask.
            state.scalp_split_pending = False
            return [
                Order(side, "main", ask, first_size, remaining),
                Order(side, "main", ask, second_size, remaining),
            ]
        state.scalp_split_pending = True
        state.scalp_split_size = second_size
        return [Order(side, "main", ask, first_size, remaining)]

    @staticmethod
    def _adverse_usd(state, side):
        """Signed USD move of BTC vs this window's PTB, against `side` (>0 = against)."""
        move = state.btc_move_pct
        ptb = getattr(state, "ptb_price", 0.0)
        if move is None or not ptb:
            return None
        move_usd = move / 100.0 * ptb
        return -move_usd if side == "Up" else move_usd

    def _maybe_split_entry(self, state, remaining, up_ask, down_ask):
        """Second half: buy the rest only if the ask falls to the split level
        while entries are still open (same first-third deadline as the entry)."""
        if not self.split_active or not getattr(state, "scalp_split_pending", False):
            return
        if state.stop_time_left is not None or not state.bet_side:
            state.scalp_split_pending = False
            return
        if self.cancel_left_pct > 0 and remaining <= self.cancel_left_seconds():
            state.scalp_split_pending = False
            state.notes.append("split_second_expired_unfilled")
            return
        side = state.bet_side
        ask = up_ask if side == "Up" else down_ask
        if ask <= 0 or ask > self.split_entry_cents / 100.0 + 1e-9:
            return
        size = int(getattr(state, "scalp_split_size", 0) or 0)
        state.scalp_split_pending = False
        if size <= 0:
            return
        order = Order(side, "main", ask, size, remaining)
        order.check(remaining, ask)
        if order.filled:
            state.orders.append(order)
            state.notes.append(
                f"split_filled_{side.lower()}_{size}@{int(round(ask * 100))}"
            )

    def _record_tp_sale(self, state, side, contracts, price_cents, remaining):
        if contracts <= 0:
            return
        price = price_cents / 100.0
        state.tp_sales.append((side, int(contracts), price, remaining))
        if state.tp_side is None:
            state.tp_side = side
            state.tp_time_left = remaining
        state.tp_contracts = sum(c for _, c, _, _ in state.tp_sales)
        state.notes.append(
            f"tp_sold_{side.lower()}_{int(contracts)}@{price_cents}_at_"
            f"{format_time(remaining)}_left"
        )

    def _maybe_take_profit(self, state, remaining, up_ask, down_ask):
        if not state.take_profit or not state.bet_side:
            return
        if state.stop_time_left is not None:
            return
        side = state.bet_side
        held = state.held(side)
        if held <= 0:
            return
        bid = self._bid(side, up_ask, down_ask)
        if bid <= 0:
            return

        if self.split_active:
            self._maybe_split_take_profit(state, side, bid, remaining)
            return

        if not self.ladder_active:
            if bid + 1e-9 < state.take_profit:
                return
            self._record_tp_sale(
                state, side, held, int(round(state.take_profit * 100)), remaining
            )
            return

        # Ladder: first tranche is half the original position (floor for odd sizes);
        # the remainder stays for the final target. A direct jump to the final
        # target fills both tranches on the same tick at their own prices.
        total = state.contracts(side)
        sold = sum(c for s, c, _, _ in state.tp_sales if s == side)
        first_size = total // 2
        if sold <= 0 and first_size > 0 and bid + 1e-9 >= self.ladder_first_cents / 100.0:
            self._record_tp_sale(state, side, min(first_size, held), self.ladder_first_cents, remaining)
            held = state.held(side)
            if held <= 0:
                return
        if bid + 1e-9 >= state.take_profit:
            self._record_tp_sale(
                state, side, state.held(side), int(round(state.take_profit * 100)), remaining
            )

    def _maybe_split_take_profit(self, state, side, bid, remaining):
        """Per-tranche exits: first half at target_cents, deep second half at
        split_target_cents. Each tranche sells in full when its bid level hits."""
        tranches = [o for o in state.orders if o.side == side and o.filled]
        sold_map = getattr(state, "scalp_sold_by_order", None)
        if sold_map is None:
            sold_map = state.scalp_sold_by_order = {}
        for idx, order in enumerate(tranches):
            target = (
                self.split_target_cents
                if idx > 0 and len(tranches) > 1
                else int(round(state.take_profit * 100))
            )
            if target <= 0 or bid + 1e-9 < target / 100.0:
                continue
            unsold = order.contracts - sold_map.get(idx, 0)
            sell = min(unsold, state.held(side))
            if sell <= 0:
                continue
            self._record_tp_sale(state, side, sell, target, remaining)
            sold_map[idx] = sold_map.get(idx, 0) + sell

    def _maybe_time_exit(self, state, remaining, up_ask, down_ask):
        if self.exit_left_seconds <= 0 or remaining > self.exit_left_seconds:
            return
        if state.stop_time_left is not None or not state.bet_side:
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
        if state.stop_time_left is not None:
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
        """Split second entry, then TP / hard stop / time exit; savior last."""
        self._maybe_split_entry(state, remaining, up_ask, down_ask)
        self._maybe_take_profit(state, remaining, up_ask, down_ask)
        if state.tp_side and state.bet_side and state.held(state.bet_side) <= 0:
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
        if state.tp_sales:
            sold = "+".join(
                f"{c}@{round(p * 100)}¢" for _, c, p, _ in state.tp_sales
            )
            text += f" · TP sold {sold}"
            if state.bet_side and state.held(state.bet_side) > 0 and (self.ladder_active or self.split_active):
                text += f" · rest → {self.target_cents}¢"
        elif state.tp_side:
            text += f" · TP {state.tp_contracts} @ {round(state.take_profit * 100)}¢"
        elif state.take_profit:
            text += f" · {self.tp_text()}"
        elif state.savior_cut_cents is not None and state.stop_time_left is None:
            text += f" · savior cut {state.savior_cut_cents}¢"
        elif state.bet_side and state.held(state.bet_side) > 0:
            text += " · hold"
        return text
