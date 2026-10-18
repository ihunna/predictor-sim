"""against: when exactly ``against_min_streak`` same winners in a row, buy the other side.

Filter modes (``against_filter``):
- ``struct_rsi`` (default): streak must be against clear structure and RSI must agree with that structure
  (up → RSI > ``rsi_agree_up``, down → RSI < ``rsi_agree_down``). With-structure / mixed / unknown /
  RSI mid/disagree/unknown → skip.
- ``struct``: streak must be against clear structure (non-trending side); no RSI gate. With-structure /
  mixed / unknown → skip. RSI is logged only.
- ``bare``: no structure/RSI gate — every exact-N streak (still skip not-broken). Structure/RSI are logged.
- ``rsi``: no structure gate — use RSI as trend and do not fade against it (bet Up only if RSI >
  ``rsi_agree_up``, bet Down only if RSI < ``rsi_agree_down``). Mid/unknown RSI → skip.

One bet per streak: a streak that keeps going is skipped until it breaks. ``min_move_vs_avg`` (default 0 = off)
can require the last move to be that many times the average of the last ``avg_windows``; it is always logged.
``take_profit_all`` puts a resting sell limit on every bet; ``take_profit_cents=0`` turns it off."""
from logs import format_time
from .combo import ComboSim

# RSI must lean with structure (struct_rsi) or with the bet side (rsi filter).
RSI_AGREE_UP = 55
RSI_AGREE_DOWN = 45
AGAINST_FILTERS = ("struct_rsi", "struct", "bare", "rsi")


class AgainstSim(ComboSim):
    def __init__(self, *args, take_profit_cents=85, take_profit_all=False, min_move_vs_avg=0.0, avg_windows=24,
                 rsi_windows=14, rsi_agree_up=RSI_AGREE_UP, rsi_agree_down=RSI_AGREE_DOWN,
                 against_filter="struct_rsi", **kwargs):
        kwargs.pop("trend_windows", None)
        if against_filter not in AGAINST_FILTERS:
            raise ValueError(f"unknown against_filter {against_filter!r}; want one of {AGAINST_FILTERS}")
        super().__init__(*args, strategy="against", trend_windows=0, avg_windows=avg_windows,
                         rsi_windows=rsi_windows, **kwargs)
        self.take_profit_cents = take_profit_cents
        self.take_profit_all = take_profit_all
        self.min_move_vs_avg = min_move_vs_avg
        self.rsi_agree_up = rsi_agree_up
        self.rsi_agree_down = rsi_agree_down
        self.against_filter = against_filter

    def summarize(self):
        capital = f"capital ${self.capital:.2f} ({self.capital_source})"
        vs_avg = (f" when the last window moved at least {self.min_move_vs_avg:g}x the average of the last "
                  f"{self.avg_windows} windows" if self.min_move_vs_avg > 0 else "")
        if self.against_filter == "bare":
            gate = "with no structure/RSI filter (bare exact-streak)"
            skip = "not-broken streaks only"
        elif self.against_filter == "rsi":
            gate = (f"with RSI-as-trend only (buy Up only if RSI>{self.rsi_agree_up:g}, "
                    f"Down only if RSI<{self.rsi_agree_down:g}; no structure gate)")
            skip = "RSI mid/unknown / fade-against-RSI"
        elif self.against_filter == "struct":
            gate = "against the price structure only (non-trending side; no RSI gate)"
            skip = "trending-side streak / mixed / unknown structure"
        else:
            gate = (f"against the price structure (non-trending side) with RSI agreeing "
                    f"(up>{self.rsi_agree_up:g} / down<{self.rsi_agree_down:g})")
            skip = "trending-side streak / mixed / unknown / RSI mid or disagree"
        return (
            f"Against [{self.against_filter}]: at exactly {self.against_min_streak} same winners in a row "
            f"{gate}{vs_avg}, buy {self.contracts} of the other side (≤{self.against_max_cents}¢); "
            f"{skip} → skip; a streak that keeps going is not bet again until it breaks; "
            f"{self._take_profit_text()}; {self._savior_text()} | {capital}"
        )

    def _take_profit_text(self):
        if self.take_profit_cents <= 0 or not self.take_profit_all:
            return "no take-profit, held to the end"
        return f"take-profit: sell all at {self.take_profit_cents}¢, else held to the end"

    def _savior_text(self):
        return self.savior_summary_text()

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
        reason = f"filter_{self.against_filter}_streak_{n}_{side.lower()}_move_{prev_move_pct:+.3f}pct"
        if vs_avg is not None:
            reason += f"_{vs_avg:.2f}x_avg"
        if self.min_move_vs_avg > 0:
            if vs_avg is None:
                return "skip", f"{reason}_avg_move_unknown"
            if vs_avg < self.min_move_vs_avg - 1e-9:
                return "skip", reason
        # Always tag structure/RSI in the reason when known (for log comparison across filters).
        if structure:
            reason += f"_structure_{structure}"
        if rsi is not None:
            reason += f"_rsi_{rsi:.0f}"

        bet = "Down" if side == "Up" else "Up"
        if self.against_filter == "bare":
            pass  # exact-N only; structure/RSI logged above
        elif self.against_filter == "rsi":
            # RSI = trend: only fade when the bet agrees with RSI (don't bet against the RSI trend).
            if rsi is None:
                return "skip", f"{reason}_rsi_unknown"
            if bet == "Up":
                if rsi > self.rsi_agree_up + 1e-9:
                    pass
                elif rsi < self.rsi_agree_down - 1e-9:
                    return "skip", f"{reason}_rsi_against_trend"
                else:
                    return "skip", f"{reason}_rsi_mid"
            else:  # bet Down
                if rsi < self.rsi_agree_down - 1e-9:
                    pass
                elif rsi > self.rsi_agree_up + 1e-9:
                    return "skip", f"{reason}_rsi_against_trend"
                else:
                    return "skip", f"{reason}_rsi_mid"
        elif self.against_filter == "struct":
            if not structure:
                return "skip", f"{reason}_structure_unknown"
            if structure == "mixed":
                return "skip", reason
            if structure == side.lower():
                return "skip", f"{reason}_with_structure"
            # clear against-structure; RSI logged only
        else:  # struct_rsi (default)
            if not structure:
                return "skip", f"{reason}_structure_unknown"
            if structure == "mixed":
                return "skip", reason
            # only fade streaks on the non-trending side; skip streaks that go with the structure
            if structure == side.lower():
                return "skip", f"{reason}_with_structure"
            if rsi is None:
                return "skip", f"{reason}_rsi_unknown"
            if structure == "up":
                if rsi > self.rsi_agree_up + 1e-9:
                    pass  # RSI agrees with up structure
                elif rsi < self.rsi_agree_down - 1e-9:
                    return "skip", f"{reason}_rsi_disagree"
                else:
                    return "skip", f"{reason}_rsi_mid"
            else:  # down
                if rsi < self.rsi_agree_down - 1e-9:
                    pass  # RSI agrees with down structure
                elif rsi > self.rsi_agree_up + 1e-9:
                    return "skip", f"{reason}_rsi_disagree"
                else:
                    return "skip", f"{reason}_rsi_mid"
        take_profit = self.take_profit_cents > 0 and self.take_profit_all
        if take_profit:
            reason += f"_tp_{self.take_profit_cents}c"
        if here:
            state.bet_side = bet
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

    def _position_exits(self, state, now, remaining, up_ask, down_ask):
        """Take-profit first; shared savior only if still holding one side."""
        self._maybe_take_profit(state, remaining, up_ask, down_ask)
        if state.tp_side:
            return
        super()._position_exits(state, now, remaining, up_ask, down_ask)

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
        elif state.savior_cut_cents is not None and state.stop_time_left is None:
            text += f" · savior cut {state.savior_cut_cents}¢"
        return text
