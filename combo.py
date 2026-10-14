"""Streak strategies, built on the dual (dual.py), logged to the same trades format.

regime: exactly 2 same winners in a row -> buy the favourite (the side with the higher ask) at its ask and hold;
else the dual if one of the last few windows was a dual (both sides reached the limit); else skip.
fav: only the favourite bets, never a dual. against: only buy the side against a streak of 3+ when the last window
moved enough. combo: fav and against together."""
import csv
import os

from dual import DualSim, Order

STRATEGIES = ("regime", "fav", "against", "combo")


class ComboSim(DualSim):
    def __init__(self, *args, strategy="combo", fav_max_cents=55, dual_lookback=3, against_min_streak=3,
                 against_min_move_pct=0.02, against_max_cents=99, fav_max_move_pct=0.02, **kwargs):
        super().__init__(*args, **kwargs)
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {strategy!r}")
        self.strategy = strategy
        self.fav_max_move_pct = fav_max_move_pct
        self.against_min_streak = against_min_streak
        self.against_min_move_pct = against_min_move_pct
        self.against_max_cents = against_max_cents
        self.fav_max_cents = fav_max_cents
        self.dual_lookback = dual_lookback
        self.history = {}  # window_start -> [outcome or None, dual (both sides reached the limit) or None]

    def summarize(self):
        capital = f"capital ${self.capital:.2f} ({self.capital_source})"
        if self.strategy == "combo":
            return (
                f"🎯 Favourite + against: after exactly 2 same winners in a row buy {self.contracts} of the favourite "
                f"(≤{self.fav_max_cents}¢{self._fav_skip_text()}); after {self.against_min_streak}+ in a row, when "
                f"the last window moved {self.against_min_move_pct:.2f}%+, buy {self.contracts} of the other side "
                f"(≤{self.against_max_cents}¢); held to the end; else skip | {capital}"
            )
        if self.strategy == "against":
            return (
                f"🎯 Against the streak: after {self.against_min_streak}+ same winners in a row, when the last window "
                f"moved {self.against_min_move_pct:.2f}%+, buy {self.contracts} of the other side "
                f"(≤{self.against_max_cents}¢, held to the end); else skip | {capital}"
            )
        if self.strategy == "fav":
            return (
                f"🎯 Favourite only: after exactly 2 same winners in a row buy {self.contracts} of the favourite "
                f"(≤{self.fav_max_cents}¢{self._fav_skip_text()}, held to the end); else skip | {capital}"
            )
        return (
            f"🎯 Regime: after exactly 2 same winners in a row buy {self.contracts} of the favourite "
            f"(≤{self.fav_max_cents}¢{self._fav_skip_text()}, held to the end); else a {self.limit_cents}¢ dual × "
            f"{self.contracts} if one of the last {self.dual_lookback} windows was a dual; else skip | dual windows: "
            f"{self.dual_rules_text()} | {capital}"
        )

    def _fav_skip_text(self):
        if self.fav_max_move_pct <= 0:
            return ""
        return f", only when the last window moved under {self.fav_max_move_pct:.2f}%"

    def load_history(self, path):
        """Fills the window history (winner, dual or not) from an existing trades log."""
        if not path or not os.path.isfile(path):
            return
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    start = int(row["window_start"])
                except (KeyError, TypeError, ValueError):
                    continue
                outcome = row.get("outcome")
                try:
                    dual = int(row["lowest_up"]) <= self.limit_cents and int(row["lowest_down"]) <= self.limit_cents
                except (KeyError, TypeError, ValueError):
                    dual = None
                self.history[start] = [outcome if outcome in ("Up", "Down", "Tie") else None, dual]

    def _streak_before(self, window_start):
        """(count, side) of the same winner in a row ending with the last window; None when not known."""
        step = self.duration * 60
        last = self.history.get(window_start - step)
        if not last or last[0] is None:
            return None
        if last[0] == "Tie":
            return 1, "Tie"
        count, start = 1, window_start - 2 * step
        while count < 50:
            entry = self.history.get(start)
            if not entry or entry[0] is None:
                return None
            if entry[0] != last[0]:
                break
            count, start = count + 1, start - step
        return count, last[0]

    def _fav_unless_move(self, streak, prev_move_pct):
        """The favourite bet, only when the last window moved less than ``fav_max_move_pct``% (0 = any move)."""
        reason = f"streak_2_{streak[1].lower()}"
        if self.fav_max_move_pct <= 0:
            return "fav", reason
        if prev_move_pct is None:
            return "skip", f"{reason}_prev_move_unknown"
        if abs(prev_move_pct) >= self.fav_max_move_pct:
            return "skip", f"{reason}_move_{prev_move_pct:+.3f}pct"
        return "fav", reason

    def regime_decision(self, window_start, prev_outcome=None, prev_move_pct=None):
        """("dual" | "fav" | "against" | "skip", reason) from the recent windows, and logs the inputs on the current
        state: exactly 2 same winners in a row -> buy the favourite; else dual if one of the last ``dual_lookback``
        windows was a dual; else skip (BTC is trending). The "against" strategy only buys the side against a streak
        of ``against_min_streak``+ when the last window moved ``against_min_move_pct``% or more."""
        step = self.duration * 60
        if prev_outcome in ("Up", "Down", "Tie"):
            self.history.setdefault(window_start - step, [None, None])[0] = prev_outcome
        streak = self._streak_before(window_start)
        duals = [self.history.get(window_start - i * step, [None, None])[1] for i in range(1, self.dual_lookback + 1)]
        known = [d for d in duals if d is not None]
        state = self.current
        if state is not None and state.window_start == window_start:
            state.streak_before = f"{streak[0]} {streak[1]}" if streak else "unknown"
            state.recent_duals = f"{sum(known)}/{len(duals)}" + ("" if len(known) == len(duals) else "?")
        if streak is None:
            return "skip", "streak_unknown"
        if self.strategy == "combo" and streak[0] == 2:
            return self._fav_unless_move(streak, prev_move_pct)
        if self.strategy in ("against", "combo"):
            n, side = streak
            if side == "Tie" or n < self.against_min_streak:
                return "skip", f"streak_{n}_{side.lower()}"
            if prev_move_pct is None:
                return "skip", "prev_move_unknown"
            if abs(prev_move_pct) < self.against_min_move_pct:
                return "skip", f"streak_{n}_{side.lower()}_move_{prev_move_pct:+.3f}pct"
            if state is not None and state.window_start == window_start:
                state.bet_side = "Down" if side == "Up" else "Up"
            return "against", f"streak_{n}_{side.lower()}_move_{prev_move_pct:+.3f}pct"
        if streak[0] == 2:
            return self._fav_unless_move(streak, prev_move_pct)
        if self.strategy == "fav":
            return "skip", f"streak_{streak[0]}_{streak[1].lower()}"
        if any(known):
            return "dual", f"dual_in_last_{self.dual_lookback}"
        if len(known) == len(duals):
            return "skip", f"no_dual_in_last_{self.dual_lookback}"
        return "skip", "recent_duals_unknown"

    def _first_orders(self, state, remaining, up_ask, down_ask):
        """fav buys the favourite and against buys ``state.bet_side`` at its ask; dual windows place the dual."""
        if state.mode == "fav":
            if up_ask == down_ask:
                return []
            side, price = ("Up", up_ask) if up_ask > down_ask else ("Down", down_ask)
            if price > self.fav_max_cents / 100 + 1e-9:
                state.trading = False
                state.notes.append(f"fav_{side.lower()}_{round(price * 100)}c_over_{self.fav_max_cents}c")
                state.notes.append("not_traded")
                return []
            return [Order(side, "main", price, self.contracts, remaining)]
        if state.mode == "against":
            side = state.bet_side
            price = up_ask if side == "Up" else down_ask
            if price > self.against_max_cents / 100 + 1e-9:
                state.trading = False
                state.notes.append(f"against_{side.lower()}_{round(price * 100)}c_over_{self.against_max_cents}c")
                state.notes.append("not_traded")
                return []
            return [Order(side, "main", price, self.contracts, remaining)]
        return super()._first_orders(state, remaining, up_ask, down_ask)

    def finish_window(self):
        state = super().finish_window()
        if state is not None:
            dual = state.lowest_up <= self.limit + 1e-9 and state.lowest_down <= self.limit + 1e-9
            self.history.setdefault(state.window_start, [None, None])[1] = dual
        return state

    def settle(self, state, outcome, *args, **kwargs):
        if outcome in ("Up", "Down", "Tie"):
            self.history.setdefault(state.window_start, [None, None])[0] = outcome
        return super().settle(state, outcome, *args, **kwargs)

    def orders_text(self, state):
        if state.mode == "dual":
            return super().orders_text(state)
        label = "favourite" if state.mode == "fav" else "against"
        return " ".join(f"{label} {o.side} {o.contracts} @ {round(o.fill_price * 100)}¢"
                        for o in state.orders if o.filled) or f"{label} not filled"
