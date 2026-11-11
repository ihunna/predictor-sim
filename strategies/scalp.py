"""Streak-fade scalper: after exactly N same winners, buy the other side ≤entry¢.

Defaults (2026-11-11) — streak-fade as main scalp:

* **Signal:** exact streak of 2 → buy the fade side when its ask ≤30¢ (taker).
* **Re-arm:** after a failed fade (streak continues), skip until the streak breaks
  and a new exact-2 forms. Longer streaks (3,4,…) are not re-faded.
* **Filters:** adverse Δ ≤ $10 / $20 / $50; entry ≤ 150s / 450s / 900s.
* **Exit:** sell when bid ≥90¢ (else hold to settle).
* **Off:** velocity, warmup, ATR, RSI mix (no window latch). ``--scalp-streak 0``
  restores cheaper-side scalp (then use ``--scalp-target`` / ``--scalp-velocity-max``).

Bid = ``1 − other_ask``. Optional split/ladder/RSI/structure/time-exit/stop via flags.
"""

from logs import format_time
from sim import BaseSim, Order

# Warm-up blackout end / first allowed entry elapsed (seconds into the window).
SCALP_WARMUP_SECONDS = {5: 0, 15: 0, 60: 0}
# Inclusive entry deadline (streak-fade live-matched mid gate): 5m ≤150s, 15m ≤450s, 60m ≤900s.
SCALP_ENTRY_ELAPSED = {5: 150, 15: 450, 60: 900}
# Signed adverse-Δ skip caps (USD) at entry — against the side we would buy.
SCALP_DELTA_CAP_USD = {5: 10.0, 15: 20.0, 60: 50.0}
# Adverse velocity ($/min) at entry; 0 = off (streak-fade default).
SCALP_VELOCITY_MAX_USD_PER_MIN = {5: 0.0, 15: 0.0, 60: 0.0}
# 6h hourly ATR max (USD) by duration; 0 = off.
SCALP_ATR_MAX_USD = {5: 0.0, 15: 0.0, 60: 0.0}
# Resting take-profit (¢); 0 = hold to settle.
SCALP_TARGET_CENTS = {5: 90, 15: 90, 60: 90}
# Exact streak length to fade other side (0 = cheaper-side scalp).
SCALP_STREAK_MIN = 2
# Side-aware RSI good-mix (recent window lean): Up only if RSI below, Down only if above.
SCALP_RSI_MIX_UP_BELOW = 40.0
SCALP_RSI_MIX_DOWN_ABOVE = 60.0
SCALP_RSI_GOOD_MIX_DEFAULT = False  # paused 2026-11-08 (was cutting +EV volume)


def scalp_warmup_seconds(duration):
    """Warm-up blackout length in seconds (no entries while elapsed < this)."""
    return int(SCALP_WARMUP_SECONDS.get(int(duration), int(duration)))


def scalp_entry_elapsed_max(duration):
    """Last elapsed second where a new entry is still allowed (inclusive)."""
    return SCALP_ENTRY_ELAPSED.get(int(duration), max(30, int(duration) * 5))


def scalp_cancel_left_pct(duration):
    """Percent of window *left* at hard cutoff (for unfilled cancel + entry gate)."""
    window = int(duration) * 60
    elapsed_max = scalp_entry_elapsed_max(duration)
    return (window - elapsed_max) / window * 100.0


def scalp_delta_cap_usd(duration):
    """Default signed adverse-Δ cap for this duration (0 if unknown)."""
    return float(SCALP_DELTA_CAP_USD.get(int(duration), 0.0))


def scalp_velocity_max_usd_per_min(duration):
    """Default adverse velocity cap ($/min) for this duration (0 if unknown)."""
    return float(SCALP_VELOCITY_MAX_USD_PER_MIN.get(int(duration), 0.0))


def scalp_atr_max_usd(duration):
    """Default ATR6h max (USD) for this duration (0 if unknown)."""
    return float(SCALP_ATR_MAX_USD.get(int(duration), 0.0))


def scalp_target_cents(duration):
    """Default resting TP (¢) for this duration (0 = hold)."""
    return int(SCALP_TARGET_CENTS.get(int(duration), 0))


class ScalpSim(BaseSim):
    """Exact-N streak-fade → buy other side ≤entry¢; TP bid≥90¢ by default. ``--scalp-streak 0`` = cheaper-side."""

    strategy = "scalp"
    start_mode = "against"

    def __init__(self, *args, entry_cents=30, target_cents=None, ladder_first_cents=0,
                 split_entry_cents=0, split_target_cents=40, delta_cap_usd=None,
                 atr_max_usd=None, velocity_max_usd_per_min=None,
                 exit_left_seconds=0, enter_after_seconds=None,
                 stop_cents=0, stop_confirm_seconds=5, require_structure="",
                 rsi_zone=None, rsi_good_mix=None,
                 rsi_mix_up_below=None, rsi_mix_down_above=None,
                 streak_min=None, **kwargs):
        duration = args[1] if len(args) > 1 else kwargs.get("duration", 5)
        # Unfilled cancel after entry deadline.
        kwargs.setdefault("cancel_left_pct", scalp_cancel_left_pct(duration))
        kwargs.setdefault("stop_loss_seconds", 0)
        kwargs.setdefault("max_prev_move_pct", 0.0)
        super().__init__(*args, **kwargs)
        self.entry_cents = max(1, int(entry_cents))
        # 0 = hold to settle (no take-profit); None → per-duration default.
        if target_cents is None:
            target_cents = scalp_target_cents(self.duration)
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
        # Signed adverse-Δ skip (USD) at entry; 0 = off. None → per-duration default.
        if delta_cap_usd is None:
            delta_cap_usd = scalp_delta_cap_usd(self.duration)
        self.delta_cap_usd = max(0.0, float(delta_cap_usd))
        # Layer 1: 6h hourly ATR max (USD); 0 = off. None → per-duration default.
        if atr_max_usd is None:
            atr_max_usd = scalp_atr_max_usd(self.duration)
        self.atr_max_usd = max(0.0, float(atr_max_usd))
        self.atr_6h: float | None = None  # set by run.py / tests
        # Back-compat alias for older UI/tests that still read atr_24h.
        self.atr_24h = None
        # Layer 2: adverse USD per elapsed minute at entry only (no latch); 0 = off.
        if velocity_max_usd_per_min is None:
            velocity_max_usd_per_min = scalp_velocity_max_usd_per_min(self.duration)
        self.velocity_max_usd_per_min = max(0.0, float(velocity_max_usd_per_min))
        self.exit_left_seconds = max(0, int(exit_left_seconds))
        # Warm-up: no entries while elapsed < this (default 0).
        if enter_after_seconds is None:
            enter_after_seconds = scalp_warmup_seconds(self.duration)
        self.enter_after_seconds = max(0, int(enter_after_seconds))
        # Inclusive entry deadline from cancel_left; 0 cancel → full window.
        window_s = self.duration * 60
        self.entry_elapsed_max = (
            window_s if self.cancel_left_pct <= 0
            else int(round(window_s * (1.0 - self.cancel_left_pct / 100.0)))
        )
        # 0 = no hard stop
        self.stop_cents = max(0, int(stop_cents if stop_cents is not None else 0))
        self.stop_confirm_seconds = max(0, int(stop_confirm_seconds))
        self.require_structure = (require_structure or "").strip().lower()
        # None = no RSI gate; else (lo, hi) inclusive, 0–100
        self.rsi_zone = rsi_zone
        # Side-aware good-mix: Up only if RSI < up_below, Down only if RSI > down_above.
        if rsi_good_mix is None:
            rsi_good_mix = SCALP_RSI_GOOD_MIX_DEFAULT
        self.rsi_good_mix = bool(rsi_good_mix)
        self.rsi_mix_up_below = float(
            SCALP_RSI_MIX_UP_BELOW if rsi_mix_up_below is None else rsi_mix_up_below
        )
        self.rsi_mix_down_above = float(
            SCALP_RSI_MIX_DOWN_ABOVE if rsi_mix_down_above is None else rsi_mix_down_above
        )
        # Exact streak length to fade (0 = legacy buy cheaper ≤entry).
        if streak_min is None:
            streak_min = SCALP_STREAK_MIN
        self.streak_min = max(0, int(streak_min))
        # window_start -> [outcome or None, unused]
        self.history: dict = {}

    @property
    def ladder_active(self):
        return self.ladder_first_cents > 0 and self.target_cents > 0

    @property
    def split_active(self):
        return self.split_entry_cents > 0 and self.target_cents > 0 and self.contracts >= 2

    def load_updown(self, path):
        """Load BTC moves + outcomes into streak history."""
        for start, outcome in super().load_updown(path):
            entry = self.history.setdefault(start, [None, None])
            if entry[0] is None:
                entry[0] = outcome

    def load_history(self, path):
        """Seed streak history from an existing trades log (outcome column)."""
        import csv
        import os
        if not path or not os.path.isfile(path):
            return
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    start = int(row["window_start"])
                except (KeyError, TypeError, ValueError):
                    continue
                outcome = row.get("outcome")
                if outcome in ("Up", "Down", "Tie"):
                    entry = self.history.setdefault(start, [None, None])
                    if entry[0] is None:
                        entry[0] = outcome

    def settle(self, state, outcome, start_price, end_price, prev_move, maker_fee_bps=0,
               taker_fee_bps=0, notes=()):
        row = super().settle(
            state, outcome, start_price, end_price, prev_move,
            maker_fee_bps=maker_fee_bps, taker_fee_bps=taker_fee_bps, notes=notes,
        )
        if outcome in ("Up", "Down", "Tie"):
            self.history.setdefault(state.window_start, [None, None])[0] = outcome
        return row

    def _streak_before(self, window_start):
        """(count, side) ending at the previous window; None if last window unknown."""
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

    def prepare_streak(self, window_start, prev_outcome=None):
        """Update history from last outcome; set streak_before + fade side on state.

        Returns skip reason or None when a fade is armed (or streak gate is off).
        """
        step = self.duration * 60
        if prev_outcome in ("Up", "Down", "Tie"):
            self.history.setdefault(window_start - step, [None, None])[0] = prev_outcome
        state = self.current
        here = state is not None and state.window_start == window_start
        streak = self._streak_before(window_start)
        if here:
            state.streak_before = f"{streak[0]} {streak[1]}" if streak else "unknown"
            state.scalp_fade_side = None
        if self.streak_min <= 0:
            return None
        if streak is None:
            return "streak_unknown"
        n, side = streak
        if side == "Tie" or n < self.streak_min:
            return f"streak_{n}_{side.lower()}"
        if n > self.streak_min:
            # Failed fade / streak not broken — wait for a fresh exact-N after break.
            return f"streak_{n}_{side.lower()}_not_broken"
        fade = "Down" if side == "Up" else "Up"
        if here:
            state.scalp_fade_side = fade
            state.bet_side = fade
        return None

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
        if self.atr_max_usd > 0:
            tp += f", ATR6h≤${self.atr_max_usd:.0f}"
        if self.velocity_max_usd_per_min > 0:
            tp += f", velocity≤${self.velocity_max_usd_per_min:.0f}/min at entry"
        if self.delta_cap_usd > 0:
            tp += f", skip if adverse Δ>${self.delta_cap_usd:.0f} at entry"
        if self.trade_hours:
            tp += (f", trade {self.trade_hours[0]:02d}:00–{self.trade_hours[1]:02d}:00 "
                   f"(UTC{self.hours_utc_offset:+g}) only")
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
        elif self.rsi_good_mix:
            rsi_gate = (
                f"RSI mix Up<{self.rsi_mix_up_below:g} / "
                f"Down>{self.rsi_mix_down_above:g}"
            )
        else:
            rsi_gate = "any RSI"
        warmup = (
            f"enter {format_time(self.enter_after_seconds)}–"
            f"{format_time(self.entry_elapsed_max)} in"
            if self.enter_after_seconds > 0 or self.entry_elapsed_max < self.duration * 60
            else "enter from open"
        )
        cancel = (
            f"cancel no-fill @{format_time(self.cancel_left_seconds())} left"
            if self.cancel_left_pct > 0 else "never cancel"
        )
        savior = self.savior_summary_text()
        if self.streak_min > 0:
            signal = (
                f"exact streak {self.streak_min} → fade other side ask≤{self.entry_cents}¢ "
                f"x{self.contracts} (skip not-broken until streak resets)"
            )
        else:
            signal = f"buy cheaper ask≤{self.entry_cents}¢ x{self.contracts}"
        return (
            f"Scalp: {signal} when {gate}, {rsi_gate}, "
            f"{tp}, {warmup}, {time_x}, {stop}, {cancel}, {savior} "
            f"| capital ${self.capital:.2f} ({self.capital_source})"
        )

    def _elapsed(self, state, remaining):
        return (state.window_end - state.window_start) - remaining

    def _in_entry_window(self, state, remaining):
        """Warm-up ≤ elapsed ≤ entry_elapsed_max (inclusive), matching the frozen formula."""
        elapsed = self._elapsed(state, remaining)
        if self.enter_after_seconds > 0 and elapsed < self.enter_after_seconds:
            return False
        if elapsed > self.entry_elapsed_max:
            return False
        return True

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

    def _velocity_trip(self, state, side, remaining):
        """True if adverse velocity for `side` is over the cap (favorable/flat never trips)."""
        if self.velocity_max_usd_per_min <= 0:
            return False
        adverse = self._adverse_usd(state, side)
        if adverse is None or adverse <= 0:
            return False
        elapsed_min = max(self._elapsed(state, remaining), 1) / 60.0
        return adverse / elapsed_min > self.velocity_max_usd_per_min + 1e-9

    def _first_orders(self, state, remaining, up_ask, down_ask):
        if self.place_at is not None and remaining > self.place_at:
            return []
        if self.exit_left_seconds > 0 and remaining <= self.exit_left_seconds:
            return []

        entry = self.entry_cents / 100.0
        fade_side = getattr(state, "scalp_fade_side", None)
        if self.streak_min > 0:
            if not fade_side:
                # Live path should have armed via prepare_streak; if not, recompute once.
                reason = self.prepare_streak(state.window_start)
                if reason:
                    state.note(reason)
                    state.action_reason = reason
                    return []
                fade_side = getattr(state, "scalp_fade_side", None)
            if not fade_side:
                return []
            ask = up_ask if fade_side == "Up" else down_ask
            candidates = [(fade_side, ask)] if 0 < ask <= entry + 1e-9 else []
        else:
            candidates = []
            if 0 < up_ask <= entry + 1e-9:
                candidates.append(("Up", up_ask))
            if 0 < down_ask <= entry + 1e-9:
                candidates.append(("Down", down_ask))

        if not self._in_entry_window(state, remaining):
            return []
        if not self._entry_ok(state):
            return []
        if not candidates:
            return []

        side, ask = min(candidates, key=lambda x: x[1])

        # Layer 1 — macro vol: 6h hourly ATR must be ready and ≤ max (fail closed).
        if self.atr_max_usd > 0:
            atr = self.atr_6h if self.atr_6h is not None else self.atr_24h
            if atr is None or atr > self.atr_max_usd + 1e-9:
                state.note("atr_high")
                state.action_reason = "atr_high"
                return []

        # Layer 2 — adverse velocity at this entry tick only (no window latch).
        if self._velocity_trip(state, side, remaining):
            state.note("velocity_high")
            state.action_reason = "velocity_high"
            return []

        adverse = self._adverse_usd(state, side)

        # Layer 3 — signed adverse-Δ cap (USD) against the side we would buy.
        if self.delta_cap_usd > 0:
            if adverse is None or adverse > self.delta_cap_usd + 1e-9:
                state.note("adverse_delta")
                state.action_reason = "adverse_delta"
                return []

        # RSI good-mix: fade recent regime (Up only if oversold lean, Down if overbought).
        if self.rsi_good_mix:
            rsi = state.rsi
            if rsi is None:
                state.note("rsi_unknown")
                state.action_reason = "rsi_unknown"
                return []
            if side == "Up" and not (rsi < self.rsi_mix_up_below - 1e-12):
                state.note("rsi_mix")
                state.action_reason = "rsi_mix"
                return []
            if side == "Down" and not (rsi > self.rsi_mix_down_above + 1e-12):
                state.note("rsi_mix")
                state.action_reason = "rsi_mix"
                return []

        state.mode = "against"
        state.bet_side = side
        state.take_profit = self.target_cents / 100.0 if self.target_cents > 0 else 0.0
        state.action = "scalp"
        struct = state.structure or "any"
        rsi_bit = f"_rsi_{state.rsi:.0f}" if state.rsi is not None else ""
        streak_bit = f"_fade_{state.streak_before.replace(' ', '')}" if state.streak_before else ""
        if self.ladder_active:
            state.action_reason = (
                f"buy_{side.lower()}_{int(round(ask * 100))}c_tp_ladder_"
                f"{self.ladder_first_cents}_{self.target_cents}c"
                f"_structure_{struct}{rsi_bit}{streak_bit}"
            )
        elif self.target_cents > 0:
            state.action_reason = (
                f"buy_{side.lower()}_{int(round(ask * 100))}c_tp_{self.target_cents}c"
                f"_structure_{struct}{rsi_bit}{streak_bit}"
            )
        else:
            state.action_reason = (
                f"buy_{side.lower()}_{int(round(ask * 100))}c_hold_structure_{struct}"
                f"{rsi_bit}{streak_bit}"
            )
        state.notes.append(state.action_reason)
        state.scalp_stop_since = None

        if not self.split_active:
            return [Order(side, "main", ask, self.contracts, remaining)]
        first_size = self.contracts // 2
        second_size = self.contracts - first_size
        if ask <= self.split_entry_cents / 100.0 + 1e-9:
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
        while entries are still open (same hard-cutoff deadline as the entry)."""
        if not self.split_active or not getattr(state, "scalp_split_pending", False):
            return
        if state.stop_time_left is not None or not state.bet_side:
            state.scalp_split_pending = False
            return
        if state.tp_sales or state.tp_side is not None:
            state.scalp_split_pending = False
            state.notes.append("split_second_cancelled_first_exited")
            return
        if not self._in_entry_window(state, remaining):
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
