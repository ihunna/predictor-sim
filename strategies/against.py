"""against: when exactly ``against_min_streak`` same winners in a row, buy the other side only when the streak is
against the recent price structure (the weak / non-trending side). A streak that goes with the structure (trending
side), mixed structure or unknown structure is skipped. One bet per streak: a streak that keeps going is skipped
until it breaks. ``min_move_vs_avg`` (default 0 = off) can require the last move to be that many times the average
of the last ``avg_windows``; it is always logged. ``take_profit_all`` puts a resting sell limit on every bet;
``take_profit_cents=0`` turns it off. Same rules for every window duration."""
from logs import format_time
from .combo import ComboSim


class AgainstSim(ComboSim):
    def __init__(self, *args, take_profit_cents=85, take_profit_all=False, min_move_vs_avg=0.0, avg_windows=24,
                 rsi_windows=14, **kwargs):
        kwargs.pop("trend_windows", None)
        super().__init__(*args, strategy="against", trend_windows=0, avg_windows=avg_windows,
                         rsi_windows=rsi_windows, **kwargs)
        self.take_profit_cents = take_profit_cents
        self.take_profit_all = take_profit_all
        self.min_move_vs_avg = min_move_vs_avg

    def summarize(self):
        capital = f"capital ${self.capital:.2f} ({self.capital_source})"
        vs_avg = (f" when the last window moved at least {self.min_move_vs_avg:g}x the average of the last "
                  f"{self.avg_windows} windows" if self.min_move_vs_avg > 0 else "")
        return (
            f"🎯 Against: at exactly {self.against_min_streak} same winners in a row against the price structure "
            f"(non-trending side){vs_avg}, buy {self.contracts} of the other side (≤{self.against_max_cents}¢); "
            f"trending-side streak / mixed / unknown → skip; a streak that keeps going is not bet again until it "
            f"breaks; {self._take_profit_text()} | {capital}"
        )

    def _take_profit_text(self):
        if self.take_profit_cents <= 0 or not self.take_profit_all:
            return "no take-profit, held to the end"
        return f"take-profit: sell all at {self.take_profit_cents}¢, else held to the end"

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
        avg, vs_avg, rsi, structure = self.refresh_structure(
            window_start, streak[0] if streak else 0, prev_move_pct)
        if here:
            state.streak_before = f"{streak[0]} {streak[1]}" if streak else "unknown"
            state.avg_move_pct = avg
            state.move_vs_avg = vs_avg
            state.rsi = rsi
            state.structure = structure
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
        if vs_avg is not None:
            reason += f"_{vs_avg:.2f}x_avg"
        if self.min_move_vs_avg > 0:
            if vs_avg is None:
                return "skip", f"{reason}_avg_move_unknown"
            if vs_avg < self.min_move_vs_avg - 1e-9:
                return "skip", reason
        if not structure:
            return "skip", f"{reason}_structure_unknown"
        reason += f"_structure_{structure}"
        if structure == "mixed":
            return "skip", reason
        # only fade streaks on the non-trending side; skip streaks that go with the structure
        if structure == side.lower():
            return "skip", f"{reason}_with_structure"
        take_profit = self.take_profit_cents > 0 and self.take_profit_all
        if take_profit:
            reason += f"_tp_{self.take_profit_cents}c"
        if here:
            state.bet_side = "Down" if side == "Up" else "Up"
            state.take_profit = self.take_profit_cents / 100 if take_profit else 0.0
        return "against", reason

    def start_window(self, window_start, window_end, slug, market_id, now=None, skip_reason=None,
                     decision_pending=False):
        state = super().start_window(window_start, window_end, slug, market_id, now=now,
                                     skip_reason=skip_reason, decision_pending=decision_pending)
        streak = self._streak_before(window_start)
        avg, vs_avg, rsi, structure = self.refresh_structure(window_start, streak[0] if streak else 0)
        state.avg_move_pct, state.move_vs_avg, state.rsi, state.structure = avg, vs_avg, rsi, structure
        state.streak_before = f"{streak[0]} {streak[1]}" if streak else "unknown"
        return state

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
