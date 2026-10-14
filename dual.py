"""Dual-limit simulator: a limit buy on both Up and Down every window.

An order fills when its side's ask reaches its price or lower. If the ask is already at or below the price when
the order goes in, it fills at once at the ask (a taker fill); otherwise it fills at its price (maker).

Hedge: once one side fills and the other has not, the other side's order is split into a lock-in order at
``hedge_cents`` (just enough contracts to break even if that side wins) and the rest at the original limit."""
import csv
import math
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from logs import format_time

CHEAP_LEVELS = (40, 30, 20, 15, 10, 5)
RISE_LEVELS = (60, 65, 70, 75)
STOP_CHECK_SECONDS = (60, 50, 40, 30, 20)
END_SAMPLE_SECONDS = 10
SNAPSHOT_SECONDS = (10, 20, 30, 40, 50, 60, 90, 120)  # seconds into the window
SNAPSHOT_LATE_SECONDS = 5  # a snapshot is skipped if no tick came within this long after its time
BTC_MOVE_LEVEL_PCT = 0.05

TRADES_HEADER = [
    "window_start",
    "slug",
    "market_id",
    "coin",
    "duration",
    "start_price",
    "end_price",
    "prev_window_move",
    "limit_cents",
    "contracts",
    "preopen_up_ask",
    "preopen_down_ask",
    "open_up_ask",
    "open_down_ask",
    "open_time_left",
    "up_ask_10s_left",
    "down_ask_10s_left",
    *[
        name
        for seconds in SNAPSHOT_SECONDS
        for name in (f"up_ask_{seconds}s_in", f"down_ask_{seconds}s_in", f"btc_move_pct_{seconds}s_in")
    ],
    f"btc_up_{BTC_MOVE_LEVEL_PCT}pct_time_left",
    f"btc_down_{BTC_MOVE_LEVEL_PCT}pct_time_left",
    "placed_time_left",
    "up_ask_at_place",
    "down_ask_at_place",
    "lowest_up",
    "lowest_up_time_left",
    "lowest_down",
    "lowest_down_time_left",
    "up_fill_time_left",
    "down_fill_time_left",
    "up_contracts",
    "up_avg_cents",
    "down_contracts",
    "down_avg_cents",
    "hedge_cents",
    "hedge_contracts",
    "hedge_fill_time_left",
    "other_low_after_first_fill",
    "other_low_after_first_fill_time_left",
    "stop_side",
    "stop_contracts",
    "stop_price_cents",
    "stop_time_left",
    "would_stop_side",
    "would_stop_contracts",
    "would_stop_price_cents",
    "would_stop_time_left",
    "would_stop_saved",
    *[
        name
        for seconds in STOP_CHECK_SECONDS
        for name in (
            f"stop_{seconds}s_side",
            f"stop_{seconds}s_contracts",
            f"stop_{seconds}s_price_cents",
            f"stop_{seconds}s_saved",
        )
    ],
    *[
        name
        for side in ("up", "down")
        for level in CHEAP_LEVELS
        for name in (
            f"{side}_touch_{level}c_time_left",
            f"{side}_max_bid_after_{level}c",
            f"{side}_max_bid_after_{level}c_time_left",
            f"{side}_bounces_{2 * level + 1}c_after_{level}c",
        )
    ],
    *[
        name
        for side in ("up", "down")
        for level in RISE_LEVELS
        for name in (
            f"{side}_rise_{level}c_time_left",
            f"{side}_min_bid_after_{level}c",
            f"{side}_min_bid_after_{level}c_time_left",
        )
    ],
    "volume_usd",
    "volume_usd_early",
    "volume_early_time_left",
    "up_ask_size_start",
    "up_ask_depth_5c_start",
    "down_ask_size_start",
    "down_ask_depth_5c_start",
    "up_ask_depth_5c_first_fill",
    "down_ask_depth_5c_first_fill",
    "up_ask_size_avg",
    "up_ask_depth_5c_avg",
    "down_ask_size_avg",
    "down_ask_depth_5c_avg",
    "spread_cents_avg",
    "book_samples",
    "fill_type",
    "outcome",
    "fee",
    "pnl",
    "capital_before",
    "capital_after",
    "reload",
    "reloaded_total",
    "notes",
]

JOINED_LATE_SECONDS = 10
@dataclass
class CheapWatch:
    """After a side's ask first reaches `level` cents: its best bid since (1 - the other side's ask) and how many
    times that bid climbed back to 2*level+1 cents (counted again only after dropping two thirds of the way back)."""
    level: int
    touch_time_left: float | None = None
    max_bid: float = 0.0
    max_bid_time_left: float | None = None
    bounces: int = 0
    above: bool = False

    @property
    def bounce_cents(self):
        return 2 * self.level + 1

    @property
    def reset_cents(self):
        return self.level + round((self.bounce_cents - self.level) * 2 / 3)

    def update(self, remaining, ask, bid):
        if self.touch_time_left is None:
            if 0.0 < ask <= self.level / 100 + 1e-9:
                self.touch_time_left = remaining
            return
        if bid <= 0:
            return
        if bid > self.max_bid:
            self.max_bid, self.max_bid_time_left = bid, remaining
        if not self.above and bid >= self.bounce_cents / 100 - 1e-9:
            self.bounces += 1
            self.above = True
        elif self.above and bid < self.reset_cents / 100 - 1e-9:
            self.above = False


def cheap_watches():
    return {level: CheapWatch(level) for level in CHEAP_LEVELS}


@dataclass
class RiseWatch:
    """After a side's bid (1 - the other side's ask) first reaches `level` cents: its lowest bid since, and when."""
    level: int
    touch_time_left: float | None = None
    min_bid: float = float("inf")
    min_bid_time_left: float | None = None

    def update(self, remaining, bid):
        if bid <= 0:
            return
        if self.touch_time_left is None:
            if bid >= self.level / 100 - 1e-9:
                self.touch_time_left = remaining
            return
        if bid < self.min_bid:
            self.min_bid, self.min_bid_time_left = bid, remaining


def rise_watches():
    return {level: RiseWatch(level) for level in RISE_LEVELS}


def last_capital_after(path, default):
    """Starting balance on restart: the last capital_after in the trades log, else default. Returns (amount, source)."""
    if not path or not os.path.isfile(path):
        return default, "default"
    last = None
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("capital_after"):
                last = row["capital_after"]
    try:
        return (float(last), "log") if last is not None else (default, "default")
    except ValueError:
        return default, "default"


def last_reloaded_total(path):
    """Total topped up so far: the last reloaded_total in the trades log, else 0."""
    if not path or not os.path.isfile(path):
        return 0.0
    last = 0.0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                last = float(row.get("reloaded_total") or last)
            except ValueError:
                pass
    return last


def migrate_trades_file(path, old_header):
    """Rewrites the trades log in place with the current columns: older rows get "none" in new columns, and
    columns no longer written are kept at the end. Returns the header now in the file."""
    header = TRADES_HEADER + [c for c in old_header if c not in TRADES_HEADER]
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    tmp = f"{path}.tmp"
    with open(tmp, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header, restval="none")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)
    return header


def _cents(price):
    return round(price * 100) if price and price != float("inf") else ""


def _cheap_columns(side, watches):
    columns = {}
    for level, watch in watches.items():
        touched = watch.touch_time_left is not None
        columns[f"{side}_touch_{level}c_time_left"] = _secs(watch.touch_time_left)
        columns[f"{side}_max_bid_after_{level}c"] = _cents(watch.max_bid) if touched else ""
        columns[f"{side}_max_bid_after_{level}c_time_left"] = _secs(watch.max_bid_time_left)
        columns[f"{side}_bounces_{watch.bounce_cents}c_after_{level}c"] = watch.bounces if touched else ""
    return columns


def _rise_columns(side, watches):
    columns = {}
    for level, watch in watches.items():
        columns[f"{side}_rise_{level}c_time_left"] = _secs(watch.touch_time_left)
        columns[f"{side}_min_bid_after_{level}c"] = _cents(watch.min_bid) if watch.touch_time_left is not None else ""
        columns[f"{side}_min_bid_after_{level}c_time_left"] = _secs(watch.min_bid_time_left)
    return columns


def _snapshot_columns(snapshots):
    columns = {}
    for seconds in SNAPSHOT_SECONDS:
        up, down, move = snapshots.get(seconds, (0.0, 0.0, None))
        columns[f"up_ask_{seconds}s_in"] = _cents(up)
        columns[f"down_ask_{seconds}s_in"] = _cents(down)
        columns[f"btc_move_pct_{seconds}s_in"] = f"{move:+.4f}" if move is not None else ""
    return columns


def _shares(depth, key):
    return f"{depth[key]:.2f}" if depth else ""


def _secs(value):
    return int(round(value)) if value is not None else ""


@dataclass
class Order:
    side: str
    kind: str  # "main", "hedge" (lock-in) or "profit" (rest of the second side at the original limit)
    price: float
    contracts: int
    placed_time_left: float
    fill_time_left: float | None = None
    fill_price: float = 0.0
    taker: bool = False
    fresh: bool = True

    @property
    def filled(self):
        return self.fill_time_left is not None

    def check(self, remaining, ask):
        """Fills at the ask on the first look (taker) or at the order price later (maker)."""
        if self.filled or ask <= 0:
            return
        if ask <= self.price:
            self.fill_time_left = remaining
            self.fill_price = ask if self.fresh else self.price
            self.taker = self.fresh
        self.fresh = False


@dataclass
class WindowState:
    window_start: int
    window_end: int
    slug: str
    market_id: str
    placed_time_left: float | None = None
    up_ask_at_place: float = 0.0
    down_ask_at_place: float = 0.0
    lowest_up: float = float("inf")
    lowest_up_time_left: float | None = None
    lowest_down: float = float("inf")
    lowest_down_time_left: float | None = None
    orders: list = field(default_factory=list)
    hedge_time_left: float | None = None
    first_fill_side: str | None = None
    other_low_after_first: float = float("inf")
    other_low_after_first_time_left: float | None = None
    stop_checked: bool = False
    stop_side: str | None = None
    stop_contracts: int = 0
    stop_price: float = 0.0
    stop_time_left: float | None = None
    would_stop: tuple | None = None  # (side, contracts, bid, time_left) when the stop-loss is only logged
    reload: float = 0.0  # dollars topped up at the start of this window
    # seconds -> (side, contracts, bid, orders filled by then): the stop-loss rule checked at each STOP_CHECK_SECONDS
    stop_checks: dict = field(default_factory=dict)
    stop_checks_done: set = field(default_factory=set)
    open_asks: tuple | None = None  # (up, down) at the first tick with both asks
    open_time_left: float | None = None
    preopen_asks: tuple | None = None  # (up, down) of this market sampled shortly before it opened
    asks_end: tuple | None = None  # (up, down) at the first tick with END_SAMPLE_SECONDS or less left
    # seconds into the window -> (up ask, down ask, BTC's % move from the price to beat or None)
    snapshots: dict = field(default_factory=dict)
    # time left when BTC was first BTC_MOVE_LEVEL_PCT or more above / below the price to beat
    btc_up_level_time_left: float | None = None
    btc_down_level_time_left: float | None = None
    cheap_up: dict = field(default_factory=cheap_watches)
    cheap_down: dict = field(default_factory=cheap_watches)
    rise_up: dict = field(default_factory=rise_watches)
    rise_down: dict = field(default_factory=rise_watches)
    depth_start: dict | None = None
    depth_first_fill: dict | None = None
    depth_now: dict | None = None
    book_now: dict | None = None
    volume_live: float | None = None
    depth_sums: dict = field(default_factory=dict)
    depth_samples: int = 0
    spread_sum: float = 0.0
    spread_samples: int = 0
    volume_usd: float | None = None
    volume_early_usd: float | None = None
    volume_early_time_left: float | None = None
    trading: bool = True
    decision_pending: bool = False
    cancelled_time_left: float | None = None
    prev_move: float | None = None
    maker_fee_bps: int = 0
    taker_fee_bps: int = 0
    notes: list = field(default_factory=list)

    def record_book(self, depth, up_ask, down_ask):
        """One order-book sample per tick, for the window's average depth and spread."""
        self.depth_now = depth
        if depth:
            for key, value in depth.items():
                self.depth_sums[key] = self.depth_sums.get(key, 0.0) + value
            self.depth_samples += 1
        if up_ask > 0 and down_ask > 0:
            self.spread_sum += up_ask + down_ask - 1.0
            self.spread_samples += 1

    def depth_avg(self, key):
        return self.depth_sums.get(key, 0.0) / self.depth_samples if self.depth_samples else None

    def side_orders(self, side):
        rank = {"main": 0, "hedge": 1, "profit": 2}
        return sorted((o for o in self.orders if o.side == side), key=lambda o: rank[o.kind])

    def fills(self, side):
        return [o for o in self.orders if o.side == side and o.filled]

    def contracts(self, side):
        return sum(o.contracts for o in self.fills(side))

    def cost(self, side):
        return sum(o.fill_price * o.contracts for o in self.fills(side))

    def held(self, side):
        """Contracts still held: bought minus any sold by the stop-loss."""
        return self.contracts(side) - (self.stop_contracts if self.stop_side == side else 0)

    def avg_price(self, side):
        n = self.contracts(side)
        return self.cost(side) / n if n else 0.0

    def first_fill_time_left(self, side):
        times = [o.fill_time_left for o in self.fills(side)]
        return max(times) if times else None

    @property
    def hedge_order(self):
        return next((o for o in self.orders if o.kind == "hedge"), None)

    @property
    def up_fill_time_left(self):
        return self.first_fill_time_left("Up")

    @property
    def down_fill_time_left(self):
        return self.first_fill_time_left("Down")

    @property
    def up_fill_price(self):
        return self.avg_price("Up")

    @property
    def down_fill_price(self):
        return self.avg_price("Down")

    @property
    def up_taker(self):
        return any(o.taker for o in self.fills("Up"))

    @property
    def down_taker(self):
        return any(o.taker for o in self.fills("Down"))

    @property
    def fill_type(self):
        up = bool(self.fills("Up"))
        down = bool(self.fills("Down"))
        if up and down:
            return "both"
        if up:
            return "only_up"
        if down:
            return "only_down"
        return "none"


class DualSim:
    def __init__(self, coin, duration, limit_cents, contracts, place_at, trades_file, capital, capital_source,
                 max_prev_move_pct=0.10, cancel_left_pct=40.0, hedge_cents=0, stop_loss_seconds=0, stop_loss_live=False,
                 stop_loss_min_cents=0, reload_to=0.0, reloaded_total=0.0):
        self.max_prev_move_pct = max_prev_move_pct
        self.stop_loss_seconds = stop_loss_seconds
        self.stop_loss_live = stop_loss_live
        self.stop_loss_min_cents = stop_loss_min_cents
        self.reload_to = float(reload_to)
        self.reloaded_total = float(reloaded_total)
        self.cancel_left_pct = cancel_left_pct
        self.hedge_cents = hedge_cents
        self.hedge = hedge_cents / 100.0
        self.coin = coin
        self.duration = duration
        self.limit = limit_cents / 100.0
        self.limit_cents = limit_cents
        self.contracts = contracts
        self.place_at = place_at
        self.trades_file = trades_file
        self.capital = float(capital)
        self.capital_source = capital_source
        self.current = None
        self.write_lock = threading.Lock()

    def summarize(self):
        when = "at window start" if self.place_at is None else f"at {self.place_at}s left"
        skip = (
            f"skip after a last-window move of {self.max_prev_move_pct:.2f}% or more"
            if self.max_prev_move_pct > 0 else "no filters"
        )
        cancel = (
            f"cancel both if nothing has filled with {format_time(self.cancel_left_seconds())} left"
            if self.cancel_left_pct > 0 else "never cancel"
        )
        hedge = (
            f"after one side fills, lock in at {self.hedge_cents}¢ on the other"
            if self.hedge_cents > 0 else "no hedge"
        )
        if self.stop_loss_seconds <= 0:
            stop = "no stop-loss"
        elif self.stop_loss_live:
            stop = f"stop-loss at {format_time(self.stop_loss_seconds)} left"
            if self.stop_loss_min_cents > 0:
                stop += f" (only if it sells for {self.stop_loss_min_cents}¢+)"
        else:
            stop = f"stop-loss paused (logging what it would sell at {format_time(self.stop_loss_seconds)} left)"
        return (
            f"🎯 Dual: {self.limit_cents}¢ limit on Up and Down, {self.contracts} contracts each, placed {when}, "
            f"{hedge}, {cancel}, {stop}, {skip} | capital ${self.capital:.2f} ({self.capital_source})"
        )

    def hedge_contracts(self, first_cost):
        """Contracts at the hedge price that break even if that side wins: cost / (1 - hedge), rounded up."""
        return min(self.contracts, math.ceil(first_cost / (1.0 - self.hedge) - 1e-9))

    def cancel_left_seconds(self):
        """Time left at which unfilled orders are cancelled (2:00 left of a 5m window)."""
        return self.duration * 60 * self.cancel_left_pct / 100.0

    def prev_move_skip_reason(self, prev_move_pct):
        """Skip reason when the last window moved ``max_prev_move_pct``% or more, or its move is unknown;
        None to trade."""
        if self.max_prev_move_pct <= 0:
            return None
        if prev_move_pct is None:
            return "prev_move_unknown"
        if abs(prev_move_pct) >= self.max_prev_move_pct:
            return f"prev_move_{prev_move_pct:+.3f}pct"
        return None

    def order_cost(self):
        """Dollars both main orders need if they fill."""
        return 2 * self.contracts * self.limit_cents / 100

    def start_window(self, window_start, window_end, slug, market_id, now=None, skip_reason=None,
                     decision_pending=False):
        """``skip_reason`` (e.g. ``no_ptb``) logs the window without trading it. ``decision_pending`` holds the
        orders (lows are still tracked) until ``resolve_decision`` is called."""
        now = time.time() if now is None else now
        state = WindowState(window_start=window_start, window_end=window_end, slug=slug, market_id=str(market_id),
                            decision_pending=decision_pending)
        if self.reload_to > 0 and self.capital < self.order_cost():
            state.reload = round(self.reload_to - self.capital, 4)
            self.capital = self.reload_to
            self.reloaded_total = round(self.reloaded_total + state.reload, 4)
            state.notes.append(f"reloaded_{state.reload:+.2f}")
        if skip_reason:
            state.trading = False
            state.notes.append(skip_reason)
            state.notes.append("not_traded")
        elif now - window_start > JOINED_LATE_SECONDS:
            state.trading = False
            state.notes.append(f"joined_late_{int(now - window_start)}s")
            state.notes.append("not_traded")
        self.current = state
        return state

    def on_tick(self, now, up_ask, down_ask, btc=0.0, ptb=0.0):
        state = self.current
        if state is None:
            return
        remaining = max(0.0, state.window_end - now)
        elapsed = now - state.window_start
        move = (btc - ptb) / ptb * 100 if btc > 0 and ptb > 0 else None
        for seconds in SNAPSHOT_SECONDS:
            if seconds not in state.snapshots and seconds <= elapsed <= seconds + SNAPSHOT_LATE_SECONDS:
                state.snapshots[seconds] = (up_ask, down_ask, move)
        if move is not None and move >= BTC_MOVE_LEVEL_PCT and state.btc_up_level_time_left is None:
            state.btc_up_level_time_left = remaining
        if move is not None and move <= -BTC_MOVE_LEVEL_PCT and state.btc_down_level_time_left is None:
            state.btc_down_level_time_left = remaining

        if 0.0 < up_ask <= 1.0 and up_ask < state.lowest_up:
            state.lowest_up = up_ask
            state.lowest_up_time_left = remaining
        if 0.0 < down_ask <= 1.0 and down_ask < state.lowest_down:
            state.lowest_down = down_ask
            state.lowest_down_time_left = remaining
        up_bid = round(1.0 - down_ask, 4) if down_ask > 0 else 0.0
        down_bid = round(1.0 - up_ask, 4) if up_ask > 0 else 0.0
        for watch in state.cheap_up.values():
            watch.update(remaining, up_ask, up_bid)
        for watch in state.cheap_down.values():
            watch.update(remaining, down_ask, down_bid)
        for watch in state.rise_up.values():
            watch.update(remaining, up_bid)
        for watch in state.rise_down.values():
            watch.update(remaining, down_bid)
        if up_ask > 0 and down_ask > 0 and state.open_asks is None:
            state.open_asks, state.open_time_left = (up_ask, down_ask), remaining
        if (up_ask > 0 or down_ask > 0) and state.asks_end is None and remaining <= END_SAMPLE_SECONDS:
            state.asks_end = (up_ask, down_ask)  # a side's ask is 0 when its book is empty (window decided)

        if (not state.trading or state.decision_pending or state.cancelled_time_left is not None
                or state.stop_time_left is not None):
            return

        cancel_left = self.cancel_left_seconds()
        if (self.cancel_left_pct > 0 and state.fill_type == "none" and state.placed_time_left is not None
                and state.placed_time_left > cancel_left and remaining <= cancel_left):
            state.cancelled_time_left = remaining
            state.notes.append(f"cancelled_no_fill_at_{format_time(remaining)}_left")
            return

        if state.placed_time_left is None:
            if up_ask <= 0 or down_ask <= 0:
                return
            if self.place_at is not None and remaining > self.place_at:
                return
            state.placed_time_left = remaining
            state.up_ask_at_place = up_ask
            state.down_ask_at_place = down_ask
            state.orders = [
                Order("Up", "main", self.limit, self.contracts, remaining),
                Order("Down", "main", self.limit, self.contracts, remaining),
            ]

        asks = {"Up": up_ask, "Down": down_ask}
        for order in state.orders:
            order.check(remaining, asks[order.side])
        self._track_other_side(state, remaining, asks)
        self._maybe_hedge(state, remaining, asks)
        self._check_stop_points(state, remaining, asks)
        self._maybe_stop(state, remaining, asks)

    def _check_stop_points(self, state, remaining, asks):
        """Logs what the stop-loss rule would sell at each STOP_CHECK_SECONDS mark (no trading)."""
        for seconds in STOP_CHECK_SECONDS:
            if seconds in state.stop_checks_done or remaining > seconds:
                continue
            if asks["Up"] <= 0 or asks["Down"] <= 0 or asks["Up"] == asks["Down"]:
                continue
            state.stop_checks_done.add(seconds)
            if state.fill_type == "none":
                continue
            leader = "Up" if asks["Up"] > asks["Down"] else "Down"
            loser = "Down" if leader == "Up" else "Up"
            _, projected = self.compute_result(state, leader, state.maker_fee_bps, state.taker_fee_bps)
            excess = state.held(loser) - state.held(leader)
            bid = round(1.0 - asks[leader], 4)
            if projected is not None and projected < 0 and excess > 0 and bid > 0:
                filled = tuple(o for o in state.orders if o.filled)
                state.stop_checks[seconds] = (loser, excess, bid, filled)

    @staticmethod
    def _pnl_of(filled, outcome, maker_fee_bps, taker_fee_bps, sold_side=None, sold=0, bid=0.0):
        cost = sum(o.fill_price * o.contracts for o in filled)
        fee = sum(o.fill_price * o.contracts * (taker_fee_bps if o.taker else maker_fee_bps) / 10000.0
                  for o in filled)
        payout = bid * sold
        fee += bid * sold * taker_fee_bps / 10000.0
        for s in ("Up", "Down"):
            held = sum(o.contracts for o in filled if o.side == s) - (sold if s == sold_side else 0)
            payout += held if outcome == s else 0.5 * held if outcome == "Tie" else 0.0
        return payout - cost - fee

    def stop_check_saved(self, state, seconds, outcome, maker_fee_bps=0, taker_fee_bps=0):
        """Pnl had the stop-loss sold at ``seconds`` left (later fills cancelled) minus the pnl of holding
        everything that actually filled with no stop-loss."""
        check = state.stop_checks.get(seconds)
        if not check or outcome not in ("Up", "Down", "Tie"):
            return None
        side, n, bid, filled = check
        stopped = self._pnl_of(filled, outcome, maker_fee_bps, taker_fee_bps, side, n, bid)
        held = self._pnl_of([o for o in state.orders if o.filled], outcome, maker_fee_bps, taker_fee_bps)
        return round(stopped - held, 4)

    def _maybe_stop(self, state, remaining, asks):
        """Once, at ``stop_loss_seconds`` left: if the window would lose with the side now leading as the winner,
        sell the losing side's unmatched contracts at its bid (1 - the leading side's ask) and cancel open orders."""
        if self.stop_loss_seconds <= 0 or state.stop_checked or remaining > self.stop_loss_seconds:
            return
        if asks["Up"] <= 0 or asks["Down"] <= 0 or asks["Up"] == asks["Down"]:
            return
        state.stop_checked = True
        if state.fill_type == "none":
            return
        leader = "Up" if asks["Up"] > asks["Down"] else "Down"
        loser = "Down" if leader == "Up" else "Up"
        _, projected = self.compute_result(state, leader, state.maker_fee_bps, state.taker_fee_bps)
        excess = state.held(loser) - state.held(leader)
        bid = round(1.0 - asks[leader], 4)
        if projected >= 0 or excess <= 0 or bid <= 0:
            return
        if self.stop_loss_live and bid < self.stop_loss_min_cents / 100 - 1e-9:
            state.would_stop = (loser, excess, bid, remaining)
            state.notes.append(f"stop_skipped_below_{self.stop_loss_min_cents}c_{loser.lower()}_{excess}@"
                               f"{round(bid * 100)}_at_{format_time(remaining)}_left")
            return
        if not self.stop_loss_live:
            state.would_stop = (loser, excess, bid, remaining)
            state.notes.append(f"would_stop_sell_{loser.lower()}_{excess}@{round(bid * 100)}_at_"
                               f"{format_time(remaining)}_left")
            return
        state.stop_side, state.stop_contracts, state.stop_price = loser, excess, bid
        state.stop_time_left = remaining
        state.notes.append(f"stop_sold_{loser.lower()}_{excess}@{round(bid * 100)}_at_{format_time(remaining)}_left")

    def _track_other_side(self, state, remaining, asks):
        """Cheapest ask of the side that did not fill first, from the first fill on (shows how close it came)."""
        if state.first_fill_side is None:
            filled = [s for s in ("Up", "Down") if state.fills(s)]
            if len(filled) != 1:
                return
            state.first_fill_side = filled[0]
        other = "Down" if state.first_fill_side == "Up" else "Up"
        ask = asks[other]
        if 0.0 < ask < state.other_low_after_first:
            state.other_low_after_first = ask
            state.other_low_after_first_time_left = remaining

    def _maybe_hedge(self, state, remaining, asks):
        """First fill on one side only: split the other side's main order into lock-in + profit orders."""
        if self.hedge_cents <= 0 or state.hedge_time_left is not None or self.hedge <= self.limit:
            return
        mains = {o.side: o for o in state.orders if o.kind == "main"}
        filled = [o for o in mains.values() if o.filled]
        if len(filled) != 1:
            return
        first = filled[0]
        other = mains["Down" if first.side == "Up" else "Up"]
        bps = state.taker_fee_bps if first.taker else state.maker_fee_bps
        first_cost = first.fill_price * first.contracts * (1 + bps / 10000.0)
        k = self.hedge_contracts(first_cost)
        state.hedge_time_left = remaining
        hedge = Order(other.side, "hedge", self.hedge, k, remaining)
        other.kind = "profit"
        other.contracts -= k
        if other.contracts <= 0:
            state.orders.remove(other)
        state.orders.append(hedge)
        hedge.check(remaining, asks[other.side])
        state.notes.append(f"hedge_{other.side.lower()}_{k}@{self.hedge_cents}")

    def resolve_decision(self, state, skip_reason=None):
        """Releases held orders, or turns the window into a logged-only one when ``skip_reason`` is given."""
        if skip_reason and state.trading:
            state.trading = False
            state.notes.append(skip_reason)
            state.notes.append("not_traded")
        state.decision_pending = False

    @staticmethod
    def would_stop_saved(state, outcome, taker_fee_bps=0):
        """What the logged-only stop-loss would have changed the pnl by: sale proceeds minus fee, minus what those
        contracts paid at settlement. None when it would not have fired or the outcome is unknown."""
        if not state.would_stop or outcome not in ("Up", "Down", "Tie"):
            return None
        side, n, bid, _ = state.would_stop
        proceeds = bid * n * (1 - taker_fee_bps / 10000.0)
        paid = n if outcome == side else 0.5 * n if outcome == "Tie" else 0.0
        return round(proceeds - paid, 4)

    def _stop_check_columns(self, state, outcome, maker_fee_bps, taker_fee_bps):
        columns = {}
        for seconds in STOP_CHECK_SECONDS:
            check = state.stop_checks.get(seconds)
            saved = self.stop_check_saved(state, seconds, outcome, maker_fee_bps, taker_fee_bps)
            columns[f"stop_{seconds}s_side"] = check[0] if check else ""
            columns[f"stop_{seconds}s_contracts"] = check[1] if check else ""
            columns[f"stop_{seconds}s_price_cents"] = _cents(check[2]) if check else ""
            columns[f"stop_{seconds}s_saved"] = f"{saved:+.4f}" if saved is not None else ""
        return columns

    def finish_window(self):
        state, self.current = self.current, None
        return state

    def compute_result(self, state, outcome, maker_fee_bps=0, taker_fee_bps=0):
        """(fee, pnl) for a finished window; pnl is None when the outcome is unknown."""
        fee = 0.0
        cost = 0.0
        payout = 0.0
        for order in state.orders:
            if not order.filled:
                continue
            cost += order.fill_price * order.contracts
            bps = taker_fee_bps if order.taker else maker_fee_bps
            fee += order.fill_price * order.contracts * bps / 10000.0
        if state.stop_side:
            sale = state.stop_price * state.stop_contracts
            payout += sale
            fee += sale * taker_fee_bps / 10000.0
        for side in ("Up", "Down"):
            if outcome == side:
                payout += state.held(side)
            elif outcome == "Tie":
                payout += 0.5 * state.held(side)
        if outcome not in ("Up", "Down", "Tie"):
            return round(fee, 4), None
        return round(fee, 4), round(payout - cost - fee, 4)

    def settle(self, state, outcome, start_price, end_price, prev_move, maker_fee_bps=0, taker_fee_bps=0, notes=()):
        """Writes the trade row and moves the balance. Returns the row dict."""
        fee, pnl = self.compute_result(state, outcome, maker_fee_bps, taker_fee_bps)
        hedge = state.hedge_order
        saved = self.would_stop_saved(state, outcome, taker_fee_bps)
        all_notes = list(state.notes) + list(notes)
        if state.up_taker:
            all_notes.append("up_taker")
        if state.down_taker:
            all_notes.append("down_taker")
        if state.trading and state.placed_time_left is None:
            all_notes.append("never_placed")
        if pnl is None:
            all_notes.append("unsettled")

        with self.write_lock:
            capital_before = self.capital
            if pnl is not None:
                self.capital = round(self.capital + pnl, 4)
            row = {
                "window_start": state.window_start,
                "slug": state.slug,
                "market_id": state.market_id,
                "coin": self.coin,
                "duration": self.duration,
                "start_price": f"{start_price:.4f}" if start_price else "",
                "end_price": f"{end_price:.4f}" if end_price else "",
                "prev_window_move": f"{prev_move:+.4f}" if prev_move is not None else "",
                "limit_cents": self.limit_cents,
                "contracts": self.contracts,
                "preopen_up_ask": _cents(state.preopen_asks[0]) if state.preopen_asks else "",
                "preopen_down_ask": _cents(state.preopen_asks[1]) if state.preopen_asks else "",
                "open_up_ask": _cents(state.open_asks[0]) if state.open_asks else "",
                "open_down_ask": _cents(state.open_asks[1]) if state.open_asks else "",
                "open_time_left": _secs(state.open_time_left),
                "up_ask_10s_left": _cents(state.asks_end[0]) if state.asks_end else "",
                "down_ask_10s_left": _cents(state.asks_end[1]) if state.asks_end else "",
                **_snapshot_columns(state.snapshots),
                f"btc_up_{BTC_MOVE_LEVEL_PCT}pct_time_left": _secs(state.btc_up_level_time_left),
                f"btc_down_{BTC_MOVE_LEVEL_PCT}pct_time_left": _secs(state.btc_down_level_time_left),
                "placed_time_left": _secs(state.placed_time_left),
                "up_ask_at_place": _cents(state.up_ask_at_place),
                "down_ask_at_place": _cents(state.down_ask_at_place),
                "lowest_up": _cents(state.lowest_up),
                "lowest_up_time_left": _secs(state.lowest_up_time_left),
                "lowest_down": _cents(state.lowest_down),
                "lowest_down_time_left": _secs(state.lowest_down_time_left),
                "up_fill_time_left": _secs(state.up_fill_time_left),
                "down_fill_time_left": _secs(state.down_fill_time_left),
                "up_contracts": state.contracts("Up"),
                "up_avg_cents": f"{state.avg_price('Up') * 100:.1f}" if state.contracts("Up") else "",
                "down_contracts": state.contracts("Down"),
                "down_avg_cents": f"{state.avg_price('Down') * 100:.1f}" if state.contracts("Down") else "",
                "hedge_cents": self.hedge_cents if hedge else "",
                "hedge_contracts": hedge.contracts if hedge else "",
                "hedge_fill_time_left": _secs(hedge.fill_time_left) if hedge else "",
                "other_low_after_first_fill": _cents(state.other_low_after_first),
                "other_low_after_first_fill_time_left": _secs(state.other_low_after_first_time_left),
                "stop_side": state.stop_side or "",
                "stop_contracts": state.stop_contracts if state.stop_side else "",
                "stop_price_cents": _cents(state.stop_price) if state.stop_side else "",
                "stop_time_left": _secs(state.stop_time_left),
                "would_stop_side": state.would_stop[0] if state.would_stop else "",
                "would_stop_contracts": state.would_stop[1] if state.would_stop else "",
                "would_stop_price_cents": _cents(state.would_stop[2]) if state.would_stop else "",
                "would_stop_time_left": _secs(state.would_stop[3]) if state.would_stop else "",
                "would_stop_saved": f"{saved:+.4f}" if saved is not None else "",
                **self._stop_check_columns(state, outcome, maker_fee_bps, taker_fee_bps),
                **_cheap_columns("up", state.cheap_up),
                **_cheap_columns("down", state.cheap_down),
                **_rise_columns("up", state.rise_up),
                **_rise_columns("down", state.rise_down),
                "volume_usd": f"{state.volume_usd:.2f}" if state.volume_usd is not None else "",
                "volume_usd_early": f"{state.volume_early_usd:.2f}" if state.volume_early_usd is not None else "",
                "volume_early_time_left": _secs(state.volume_early_time_left),
                "up_ask_size_start": _shares(state.depth_start, "up_ask_size"),
                "up_ask_depth_5c_start": _shares(state.depth_start, "up_ask_depth"),
                "down_ask_size_start": _shares(state.depth_start, "down_ask_size"),
                "down_ask_depth_5c_start": _shares(state.depth_start, "down_ask_depth"),
                "up_ask_depth_5c_first_fill": _shares(state.depth_first_fill, "up_ask_depth"),
                "down_ask_depth_5c_first_fill": _shares(state.depth_first_fill, "down_ask_depth"),
                **{
                    f"{key.replace('_depth', '_depth_5c')}_avg": (
                        f"{state.depth_avg(key):.2f}" if state.depth_samples else ""
                    )
                    for key in ("up_ask_size", "up_ask_depth", "down_ask_size", "down_ask_depth")
                },
                "spread_cents_avg": (
                    f"{state.spread_sum / state.spread_samples * 100:.2f}" if state.spread_samples else ""
                ),
                "book_samples": state.depth_samples,
                "fill_type": state.fill_type,
                "outcome": outcome,
                "fee": f"{fee:.4f}",
                "pnl": f"{pnl:.4f}" if pnl is not None else "",
                "capital_before": f"{capital_before:.4f}",
                "capital_after": f"{self.capital:.4f}",
                "reload": f"{state.reload:.4f}" if state.reload else "",
                "reloaded_total": f"{self.reloaded_total:.4f}",
                "notes": ";".join(all_notes),
            }
            self._write_row(row)
        return row

    def _write_row(self, row):
        path = self.trades_file
        header = TRADES_HEADER
        if os.path.isfile(path):
            with open(path, newline="", encoding="utf-8") as f:
                old_header = next(csv.reader(f), None)
            if old_header and old_header != TRADES_HEADER:
                header = migrate_trades_file(path, old_header)
        file_exists = os.path.isfile(path)
        with open(path, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=header, restval="none")
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)

    def display_status(self):
        state = self.current
        if state is None:
            return f"Dual: idle | ${self.capital:.2f}"
        if not state.trading:
            orders = f"not trading ({state.notes[0]})"
        elif state.decision_pending:
            orders = "checking last window"
        elif state.placed_time_left is None:
            orders = "waiting to place"
        else:
            up = "✅" if state.up_fill_time_left is not None else "⏳"
            down = "✅" if state.down_fill_time_left is not None else "⏳"
            orders = f"Up{up} Down{down}"
        return f"Dual {self.limit_cents}¢: {orders} | ${self.capital:.2f}"
