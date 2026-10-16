"""Streak strategies (regime / fav / combo), built on BaseSim (sim.py).

regime: exactly 2 same winners in a row -> buy the favourite (the side with the higher ask) at its ask and hold;
else the dual if one of the last few windows was a dual (both sides reached the limit); else skip.
fav: only the favourite bets, never a dual. against (against.py): its own rules. combo: streaks of 3+ only. With a
trend (the side with more 3+ streaks in the last trend_windows), a streak against the trend is bet to break after a
big enough last move, a streak with the trend is bet to continue after a small and slowing last move, else skip;
trend_windows=0 uses against plus continue without a trend."""
import csv
import os
import re

from sim import BaseSim, Order

STRATEGIES = ("regime", "fav", "against", "combo")


class ComboSim(BaseSim):
    def __init__(self, *args, strategy="combo", fav_max_cents=55, dual_lookback=3, against_min_streak=3,
                 against_min_move_pct=0.02, against_max_cents=99, fav_max_move_pct=0.02, continue_max_move_pct=0.05,
                 trend_windows=96, trend_against_min_move_pct=0.05, against_stop_after_loss=True,
                 avg_windows=24, rsi_windows=14, **kwargs):
        super().__init__(*args, avg_windows=avg_windows, rsi_windows=rsi_windows, **kwargs)
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {strategy!r}")
        self.strategy = strategy
        self.fav_max_move_pct = fav_max_move_pct
        self.against_min_streak = against_min_streak
        self.against_min_move_pct = against_min_move_pct
        self.against_max_cents = against_max_cents
        self.continue_max_move_pct = continue_max_move_pct
        self.trend_windows = trend_windows
        self.trend_against_min_move_pct = trend_against_min_move_pct
        self.against_stop_after_loss = against_stop_after_loss
        self.fav_max_cents = fav_max_cents
        self.dual_lookback = dual_lookback
        self.history = {}  # window_start -> [outcome or None, dual (both sides reached the limit) or None]
        self.against_streaks = {}  # (streak start, side) -> first window bet against it

    def summarize(self):
        capital = f"capital ${self.capital:.2f} ({self.capital_source})"
        if self.strategy == "combo":
            return (
                f"🎯 Streaks: after {self.against_min_streak}+ same winners in a row, when the last window moved "
                f"{self.against_min_move_pct:.2f}%+, buy {self.contracts} of the other side "
                f"(≤{self.against_max_cents}¢){self._continue_text()}; held to the end; else skip | {capital}"
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

    def _continue_text(self):
        text = ""
        if self.continue_max_move_pct > 0:
            text = (f"; but when the last move was under {self.continue_max_move_pct:.2f}% and smaller than the one "
                    f"before, buy the streak's side instead")
        if self.trend_windows > 0:
            text = (f"; trend = the side with more 3+ streaks in the last {self.trend_windows} windows: a streak "
                    f"against the trend → bet it breaks when the last move was {self.trend_against_min_move_pct:.2f}%+"
                    + (" (once per streak: no more after a loss)" if self.against_stop_after_loss else "")
                    + f"; a streak with the trend → bet it continues when the last move was under "
                    f"{self.continue_max_move_pct:.2f}% and smaller than the one before; no trend → skip")
        return text

    def trend_before(self, streak_start):
        """(trending side or None for a tie, up streaks, down streaks) from the ``trend_windows`` windows before
        ``streak_start``, counting 3+ runs of the same winner (an unknown window ends a run); None when more than
        a tenth of those windows are not known."""
        step = self.duration * 60
        outcomes = []
        for i in range(self.trend_windows, 0, -1):
            entry = self.history.get(streak_start - i * step)
            outcomes.append(entry[0] if entry else None)
        if outcomes.count(None) > self.trend_windows // 10:
            return None
        counts = {"Up": 0, "Down": 0}
        run_side, run = None, 0
        for outcome in outcomes + [None]:
            if outcome == run_side:
                run += 1
                continue
            if run_side in counts and run >= 3:
                counts[run_side] += 1
            run_side, run = outcome, 1
        up, down = counts["Up"], counts["Down"]
        return ("Up" if up > down else "Down" if down > up else None), up, down

    def _fav_skip_text(self):
        if self.fav_max_move_pct <= 0:
            return ""
        return f", only when the last window moved under {self.fav_max_move_pct:.2f}%"

    def load_updown(self, path):
        """Loads outcomes into streak history and BTC moves into the shared ``PriceStructure``."""
        for start, outcome in super().load_updown(path):
            entry = self.history.setdefault(start, [None, None])
            if entry[0] is None:
                entry[0] = outcome

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
                try:
                    self._record_move(start, float(row["start_price"]), float(row["end_price"]))
                except (KeyError, TypeError, ValueError):
                    pass
                match = re.match(r"streak_(\d+)_(up|down)_", row.get("action_reason") or "")
                if row.get("action") == "against" and match:
                    key = (start - int(match.group(1)) * self.duration * 60, match.group(2).capitalize())
                    self.against_streaks.setdefault(key, start)

    def _record_move(self, window_start, start_price, end_price):
        self.price_structure.record_move(window_start, start_price, end_price)

    def _streak_before(self, window_start):
        """(count, side) of the same winner in a row ending with the last window; None only when the last
        window itself is not known. A missing older window ends the streak at the known length (so joining
        late still counts the recent run)."""
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
                break
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
        if prev_move_pct is not None:
            self.moves[window_start - step] = prev_move_pct
        streak = self._streak_before(window_start)
        duals = [self.history.get(window_start - i * step, [None, None])[1] for i in range(1, self.dual_lookback + 1)]
        known = [d for d in duals if d is not None]
        state = self.current
        if state is not None and state.window_start == window_start:
            state.streak_before = f"{streak[0]} {streak[1]}" if streak else "unknown"
            state.recent_duals = f"{sum(known)}/{len(duals)}" + ("" if len(known) == len(duals) else "?")
        if streak is None:
            return "skip", "streak_unknown"
        if self.strategy in ("against", "combo"):
            n, side = streak
            if side == "Tie" or n < self.against_min_streak:
                return "skip", f"streak_{n}_{side.lower()}"
            if prev_move_pct is None:
                return "skip", "prev_move_unknown"
            reason = f"streak_{n}_{side.lower()}_move_{prev_move_pct:+.3f}pct"
            other = "Down" if side == "Up" else "Up"
            before = self.moves.get(window_start - 2 * step)
            if self.strategy == "combo" and self.trend_windows > 0:
                return self._trend_decision(state, window_start, n, side, prev_move_pct, before, reason)
            if (self.strategy == "combo" and self.continue_max_move_pct > 0 and before is not None
                    and abs(prev_move_pct) < self.continue_max_move_pct and abs(prev_move_pct) < abs(before)):
                if state is not None and state.window_start == window_start:
                    state.bet_side = side
                return "continue", f"{reason}_slowing_from_{before:+.3f}pct"
            if abs(prev_move_pct) < self.against_min_move_pct:
                return "skip", reason
            if state is not None and state.window_start == window_start:
                state.bet_side = "Down" if side == "Up" else "Up"
            return "against", reason
        if streak[0] == 2:
            return self._fav_unless_move(streak, prev_move_pct)
        if self.strategy == "fav":
            return "skip", f"streak_{streak[0]}_{streak[1].lower()}"
        if any(known):
            return "dual", f"dual_in_last_{self.dual_lookback}"
        if len(known) == len(duals):
            return "skip", f"no_dual_in_last_{self.dual_lookback}"
        return "skip", "recent_duals_unknown"

    def _trend_decision(self, state, window_start, n, side, prev_move_pct, before, reason):
        """A streak against the trend -> against (once per streak with ``against_stop_after_loss``: the streak is
        still going, so an earlier bet on it lost); a streak with the trend -> continue when the last move was small
        and slowing; no trend or an unknown one -> skip."""
        streak_start = window_start - n * self.duration * 60
        trend = self.trend_before(streak_start)
        if trend is None:
            return "skip", f"{reason}_trend_unknown"
        if trend[0] is None:
            return "skip", f"{reason}_no_trend_{trend[1]}v{trend[2]}"
        reason += f"_trend_{trend[0].lower()}_{trend[1]}v{trend[2]}"
        move = abs(prev_move_pct)
        if trend[0] != side:
            reason += "_against_trend"
            if move < self.trend_against_min_move_pct:
                return "skip", reason
            first = self.against_streaks.setdefault((streak_start, side), window_start)
            if self.against_stop_after_loss and first < window_start:
                return "skip", f"{reason}_lost_on_this_streak"
            action, bet = "against", "Down" if side == "Up" else "Up"
        else:
            reason += "_with_trend"
            if (self.continue_max_move_pct <= 0 or before is None or move >= self.continue_max_move_pct
                    or move >= abs(before)):
                return "skip", reason
            action, bet = "continue", side
            reason += f"_slowing_from_{before:+.3f}pct"
        if state is not None and state.window_start == window_start:
            state.bet_side = bet
        return action, reason

    def _first_orders(self, state, remaining, up_ask, down_ask):
        """fav buys the favourite, against and continue buy ``state.bet_side`` at its ask; dual windows place the
        dual."""
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
        if state.mode in ("against", "continue"):
            side = state.bet_side
            price = up_ask if side == "Up" else down_ask
            if price > self.against_max_cents / 100 + 1e-9:
                state.trading = False
                state.notes.append(f"{state.mode}_{side.lower()}_{round(price * 100)}c_over_{self.against_max_cents}c")
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

    def settle(self, state, outcome, start_price, end_price, *args, **kwargs):
        if outcome in ("Up", "Down", "Tie"):
            self.history.setdefault(state.window_start, [None, None])[0] = outcome
        self._record_move(state.window_start, start_price, end_price)
        return super().settle(state, outcome, start_price, end_price, *args, **kwargs)

    def orders_text(self, state):
        if state.mode == "dual":
            return super().orders_text(state)
        label = {"fav": "favourite", "continue": "with streak"}.get(state.mode, "against")
        return " ".join(f"{label} {o.side} {o.contracts} @ {round(o.fill_price * 100)}¢"
                        for o in state.orders if o.filled) or f"{label} not filled"
