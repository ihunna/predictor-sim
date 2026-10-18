"""Official price-structure trend for every strategy.

Uses only each window's BTC % move (official start → end). Not streak counts, not the order book.

Lookbacks are expressed in **5m-equivalent windows**, then scaled to the sim duration so one 60m
bar ≈ 12×5m ≈ 4×15m of market time:

  avg 24 / RSI 14 at 5m  →  8 / 5 at 15m  →  8 / 5 at 60m (floored so structure can still form)

Fill ``moves`` from ``load_updown`` on startup, then from trades / live settles. Every sim (dual,
leader, combo, against, …) should use one ``PriceStructure`` via ``BaseSim.price_structure``.
"""
import csv
import os


# Defaults are calibrated on 5m; longer durations use fewer bars covering similar calendar time.
BASE_DURATION_MINUTES = 5
DEFAULT_AVG_WINDOWS = 24   # 5m: 2h of history
DEFAULT_RSI_WINDOWS = 14   # 5m: ~70m of history
# Floors keep HH/HL structure meaningful on 15m/60m after scaling.
MIN_AVG_WINDOWS = 8
MIN_RSI_WINDOWS = 5


def _price(value):
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def scale_lookback(base_windows, duration_minutes, floor):
    """Map a 5m lookback count onto ``duration_minutes`` (1×60m ≈ 12×5m ≈ 4×15m)."""
    duration = max(1, int(duration_minutes))
    scaled = int(round(base_windows * BASE_DURATION_MINUTES / duration))
    return max(floor, scaled)


class PriceStructure:
    def __init__(self, duration, avg_windows=DEFAULT_AVG_WINDOWS, rsi_windows=DEFAULT_RSI_WINDOWS,
                 scale_lookbacks=True):
        self.duration = duration
        # 5m keeps the raw counts (against tests / CLI). 15m/60m scale so one bar ≈ 3×/12× 5m.
        if scale_lookbacks and int(duration) != BASE_DURATION_MINUTES:
            self.avg_windows = scale_lookback(avg_windows, duration, MIN_AVG_WINDOWS)
            self.rsi_windows = scale_lookback(rsi_windows, duration, MIN_RSI_WINDOWS)
        else:
            self.avg_windows = max(1, int(avg_windows))
            self.rsi_windows = max(1, int(rsi_windows))
        self.moves = {}  # window_start -> BTC move in %
        self.structure = ""  # "up" | "down" | "mixed" | ""
        self.avg_move_pct = None
        self.move_vs_avg = None
        self.rsi = None

    def record_move(self, window_start, start_price, end_price):
        if start_price and end_price:
            self.moves[window_start] = (end_price - start_price) / start_price * 100

    def set_move(self, window_start, move_pct):
        if move_pct is not None:
            self.moves[window_start] = move_pct

    def load_updown(self, path):
        """Load BTC moves (and return outcome rows for callers that also keep history)."""
        outcomes = []
        if not path or not os.path.isfile(path):
            return outcomes
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    start = int(row["Time stamp"])
                except (KeyError, TypeError, ValueError):
                    continue
                outcome = row.get("outcome")
                if outcome in ("Up", "Down", "Tie"):
                    outcomes.append((start, outcome))
                start_px, end_px = _price(row.get("price_to_beat")), _price(row.get("final_price"))
                if start_px and end_px and start not in self.moves:
                    self.record_move(start, start_px, end_px)
        return outcomes

    def _recent_moves(self, window_start, count):
        """Last ``count`` window moves ending before ``window_start``.

        Allows a few holes (≤ max(1, count//10) when count≥4). Longer durations request fewer
        bars via ``scale_lookback``, so 60m can form structure from the hours it actually has.
        """
        step = self.duration * 60
        moves = [self.moves.get(window_start - i * step) for i in range(count, 0, -1)]
        missing = moves.count(None)
        max_missing = max(1, count // 10) if count >= 4 else 0
        return None if missing > max_missing else moves

    def move_stats(self, window_start, streak_len=0):
        """(avg_move_pct, rsi, structure) from recent BTC moves. structure: up|down|mixed|""."""
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
        # Need two halves with at least 2 points each (scaled lookbacks are shorter than 5m's 24).
        usable = (len(moves) - streak_len) if moves else 0
        if moves and usable >= 4:
            level, points = 0.0, []
            for m in moves[:usable]:
                start, level = level, level + (m or 0.0)
                points.append((start, level))
            half = len(points) // 2
            a, b = points[:half], points[half:]
            if a and b:
                hi_a, hi_b = max(max(p) for p in a), max(max(p) for p in b)
                lo_a, lo_b = min(min(p) for p in a), min(min(p) for p in b)
                structure = ("up" if hi_b > hi_a and lo_b > lo_a else
                             "down" if hi_b < hi_a and lo_b < lo_a else "mixed")
        return avg, rsi, structure

    def refresh(self, window_start, streak_len=0, prev_move_pct=None):
        """Recompute and cache the trend; returns (avg, move_vs_avg, rsi, structure)."""
        avg, rsi, structure = self.move_stats(window_start, streak_len)
        vs_avg = abs(prev_move_pct) / avg if prev_move_pct is not None and avg else None
        self.avg_move_pct, self.move_vs_avg, self.rsi, self.structure = avg, vs_avg, rsi, structure
        return avg, vs_avg, rsi, structure
