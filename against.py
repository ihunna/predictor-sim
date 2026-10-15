"""against: when exactly ``against_min_streak`` same winners in a row and the last window moved enough, buy the other
side (one bet per streak: a streak that keeps going is skipped until it breaks). Before entry it checks the trend (the
side with more 3+ streaks in the last ``trend_windows`` windows): a streak that goes with the trend (or an unknown
trend) gets a take-profit, a resting sell limit for the whole position; ``take_profit_all`` puts it on every bet,
``take_profit_cents=0`` turns it off. Same rules for every window duration."""
from combo import ComboSim
from logs import format_time


class AgainstSim(ComboSim):
    def __init__(self, *args, take_profit_cents=85, take_profit_all=False, **kwargs):
        kwargs.setdefault("trend_windows", 96)
        super().__init__(*args, strategy="against", **kwargs)
        self.take_profit_cents = take_profit_cents
        self.take_profit_all = take_profit_all

    def summarize(self):
        capital = f"capital ${self.capital:.2f} ({self.capital_source})"
        return (
            f"🎯 Against: at exactly {self.against_min_streak} same winners in a row, when the last window moved "
            f"{self.against_min_move_pct:.2f}%+, buy {self.contracts} of the other side (≤{self.against_max_cents}¢); "
            f"a streak that keeps going is not bet again until it breaks; {self._take_profit_text()} | {capital}"
        )

    def _take_profit_text(self):
        if self.take_profit_cents <= 0:
            return "no take-profit, held to the end"
        if self.take_profit_all:
            return f"take-profit: sell all at {self.take_profit_cents}¢, else held to the end"
        return (f"streak with the trend (the side with more 3+ streaks in the last {self.trend_windows} windows) → "
                f"take-profit: sell all at {self.take_profit_cents}¢; else held to the end")

    def regime_decision(self, window_start, prev_outcome=None, prev_move_pct=None):
        """("against" | "skip", reason), and sets the side and the take-profit on the current state."""
        step = self.duration * 60
        if prev_outcome in ("Up", "Down", "Tie"):
            self.history.setdefault(window_start - step, [None, None])[0] = prev_outcome
        if prev_move_pct is not None:
            self.moves[window_start - step] = prev_move_pct
        streak = self._streak_before(window_start)
        state = self.current
        here = state is not None and state.window_start == window_start
        if here:
            state.streak_before = f"{streak[0]} {streak[1]}" if streak else "unknown"
        if streak is None:
            return "skip", "streak_unknown"
        n, side = streak
        if side == "Tie" or n < self.against_min_streak:
            return "skip", f"streak_{n}_{side.lower()}"
        if n > self.against_min_streak:
            return "skip", f"streak_{n}_{side.lower()}_not_broken"
        if prev_move_pct is None:
            return "skip", "prev_move_unknown"
        reason = f"streak_{n}_{side.lower()}_move_{prev_move_pct:+.3f}pct"
        if abs(prev_move_pct) < self.against_min_move_pct:
            return "skip", reason
        with_trend = False
        if self.trend_windows > 0:
            trend = self.trend_before(window_start - n * step)
            if trend is None:
                reason += "_trend_unknown"
                with_trend = True
            elif trend[0] is None:
                reason += f"_no_trend_{trend[1]}v{trend[2]}"
            else:
                with_trend = trend[0] == side
                reason += (f"_trend_{trend[0].lower()}_{trend[1]}v{trend[2]}_"
                           + ("with_trend" if with_trend else "against_trend"))
        take_profit = self.take_profit_cents > 0 and (with_trend or self.take_profit_all)
        if take_profit:
            reason += f"_tp_{self.take_profit_cents}c"
        if here:
            state.bet_side = "Down" if side == "Up" else "Up"
            state.take_profit = self.take_profit_cents / 100 if take_profit else 0.0
        return "against", reason

    def _trade_tick(self, state, now, remaining, up_ask, down_ask):
        super()._trade_tick(state, now, remaining, up_ask, down_ask)
        self._maybe_take_profit(state, remaining, up_ask, down_ask)

    def _maybe_take_profit(self, state, remaining, up_ask, down_ask):
        """Sells the whole position at the take-profit price once our side's bid (1 - the other side's ask) reaches
        it (a resting maker limit)."""
        if not state.take_profit or state.tp_side or state.mode != "against" or not state.bet_side:
            return
        side = state.bet_side
        held = state.held(side)
        other_ask = down_ask if side == "Up" else up_ask
        if held <= 0 or other_ask <= 0 or 1.0 - other_ask < state.take_profit - 1e-9:
            return
        state.tp_side, state.tp_contracts, state.tp_time_left = side, held, remaining
        state.notes.append(f"tp_sold_{side.lower()}_{held}@{round(state.take_profit * 100)}_at_"
                           f"{format_time(remaining)}_left")

    def orders_text(self, state):
        text = super().orders_text(state)
        if state.tp_side:
            text += f" · sold {state.tp_contracts} @ {round(state.take_profit * 100)}¢ (take-profit)"
        elif state.take_profit:
            text += f" · take-profit at {round(state.take_profit * 100)}¢"
        return text
