"""Official price-structure trend for every strategy.

Uses only each window's BTC % move (official start → end). Not streak counts, not the order book.

Fill ``moves`` from ``load_updown`` on startup, then from trades / live settles. Every sim (dual, leader,
combo, against, …) should use one ``PriceStructure`` instance via ``BaseSim.price_structure`` (sim.py).
"""
import csv
import os


def _price(value):
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


class PriceStructure:
    def __init__(self, duration, avg_windows=24, rsi_windows=14):
        self.duration = duration
        self.avg_windows = avg_windows
        self.rsi_windows = rsi_windows
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
        step = self.duration * 60
        moves = [self.moves.get(window_start - i * step) for i in range(count, 0, -1)]
        return None if moves.count(None) > count // 10 else moves

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

    def refresh(self, window_start, streak_len=0, prev_move_pct=None):
        """Recompute and cache the trend; returns (avg, move_vs_avg, rsi, structure)."""
        avg, rsi, structure = self.move_stats(window_start, streak_len)
        vs_avg = abs(prev_move_pct) / avg if prev_move_pct is not None and avg else None
        self.avg_move_pct, self.move_vs_avg, self.rsi, self.structure = avg, vs_avg, rsi, structure
        return avg, vs_avg, rsi, structure
