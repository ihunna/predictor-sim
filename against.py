"""against: when exactly ``against_min_streak`` same winners in a row and the last window moved
``min_move_vs_avg`` times the average move of the last ``avg_windows``, buy the other side only when the streak is
against the recent price structure (the weak / non-trending side). A streak that goes with the structure (trending
side), mixed structure or unknown structure is skipped. One bet per streak: a streak that keeps going is skipped
until it breaks. ``take_profit_all`` puts a resting sell limit on every bet; ``take_profit_cents=0`` turns it off.
Structure, RSI and move-vs-average are logged on every window. Same rules for every window duration."""
from combo import ComboSim
from logs import format_time


class AgainstSim(ComboSim):
    def __init__(self, *args, take_profit_cents=85, take_profit_all=False, min_move_vs_avg=1.0, avg_windows=24,
                 rsi_windows=14, **kwargs):
        kwargs.pop("trend_windows", None)
        super().__init__(*args, strategy="against", trend_windows=0, **kwargs)
        self.take_profit_cents = take_profit_cents
        self.take_profit_all = take_profit_all
        self.min_move_vs_avg = min_move_vs_avg
        self.avg_windows = avg_windows
        self.rsi_windows = rsi_windows
        self.structure = ""  # last known: "up" | "down" | "mixed" | ""
        self.avg_move_pct = None
        self.move_vs_avg = None
        self.rsi = None

    def _recent_moves(self, window_start, count):
        """The moves (%) of the ``count`` windows before ``window_start``, oldest first, None for a missing one;
        None when more than a tenth are missing."""
        step = self.duration * 60
        moves = [self.moves.get(window_start - i * step) for i in range(count, 0, -1)]
        return None if moves.count(None) > count // 10 else moves

    def move_stats(self, window_start, streak_len=0):
        """(average move size %, RSI 0-100, structure "up" | "down" | "mixed" | "") from the recent windows; the
        structure is the higher/lower highs and lows of the windows before the streak, from the summed moves."""
        moves = self._recent_moves(window_start, self.avg_windows)
        known = [m for m in moves or [] if m is not None]
        avg = sum(abs(m) for m in known) / len(known) if known else None
        rsi = None
        last = self._recent_moves(window_start, self.rsi_windows)
        if last:
            gains = sum(m for m in last if m and m > 0)
            losses = -sum(m for m in last if m and m < 0)
            rsi = 100 * gains / (gains + losses) if gains + losses else 50.0
        structure = ""
        if moves and len(moves) - streak_len > 6:
            level, points = 0.0, []
            for m in moves[:len(moves) - streak_len]:
                start, level = level, level + (m or 0.0)
                points.append((start, level))
            half = len(points) // 2
            a, b = points[:half], points[half:]
            hi_a, hi_b = max(max(p) for p in a), max(max(p) for p in b)
            lo_a, lo_b = min(min(p) for p in a), min(min(p) for p in b)
            structure = ("up" if hi_b > hi_a and lo_b > lo_a else "down" if hi_b < hi_a and lo_b < lo_a
                         else "mixed")
        return avg, rsi, structure

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
        avg, rsi, structure = self.move_stats(window_start, streak[0] if streak else 0)
        vs_avg = abs(prev_move_pct) / avg if prev_move_pct is not None and avg else None
        self.avg_move_pct, self.move_vs_avg, self.rsi, self.structure = avg, vs_avg, rsi, structure
        if here:
            state.streak_before = f"{streak[0]} {streak[1]}" if streak else "unknown"
            state.avg_move_pct, state.move_vs_avg, state.rsi, state.structure = avg, vs_avg, rsi, structure
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
        if self.min_move_vs_avg > 0:
            if vs_avg is None:
                return "skip", f"{reason}_avg_move_unknown"
            reason += f"_{vs_avg:.2f}x_avg"
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
        avg, rsi, structure = self.move_stats(window_start, streak[0] if streak else 0)
        self.avg_move_pct, self.rsi, self.structure = avg, rsi, structure
        state.avg_move_pct, state.rsi, state.structure = avg, rsi, structure
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
