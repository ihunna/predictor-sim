#!/usr/bin/env python3
"""Predict.fun dual-only simulator: a limit on both Up and Down in every window of one coin/duration.

Usage:
    python run.py --coin btc -d 5                # live dashboard (needs a terminal; use tmux/screen on a server)
    python run.py --coin btc -d 5 --plain        # plain lines, for nohup / log files
    python run.py --coin eth -d 15 --limit 30 --contracts 8 --max-prev-move-pct 0.10 --capital-last 112.40
"""
import argparse
import socket
import threading
import time

from dual import DualSim, last_capital_after
from logs import format_lowest_cents, log_to_csv
from predict_feed import (
    CHAINLINK_FEED_IDS,
    SUPPORTED_DURATIONS,
    PredictWS,
    fetch_market,
    get_current_active_slug,
    get_market_metadata_for_slug,
    window_bounds,
)
from ui import Dashboard, PlainUI, hhmm, stamp

# Some cloud hosts stall on IPv6 connects; resolve IPv4 only for this process.
_system_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _system_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo

NEXT_WINDOW_SUBSCRIBE_SECONDS = 60
TICK_SECONDS = 0.2
OFFICIAL_PRICE_WAIT_SECONDS = 10
RESOLUTION_CHECK_ATTEMPTS = 4
RESOLUTION_CHECK_POLL_SECONDS = 15


def prepare_next_window(ws, coin, duration, next_start):
    try:
        market_id, _, _ = get_market_metadata_for_slug(get_current_active_slug(coin, duration, at=next_start))
        if market_id:
            ws.prepare_next(market_id)
    except Exception:
        pass


def load_fees(state):
    info = fetch_market(state.market_id)
    if info:
        state.maker_fee_bps = info["maker_fee_bps"]
        state.taker_fee_bps = info["taker_fee_bps"]


def previous_window_prices(ws, coin, duration, window_start, timeout):
    """The previous window's official (start, end) prices from the socket; zeros when the bot never saw them
    (e.g. it started during that window or later)."""
    prev_start, _ = window_bounds(duration, window_start - 1)
    prev_market, _, _ = get_market_metadata_for_slug(get_current_active_slug(coin, duration, at=prev_start))
    if not prev_market:
        return 0.0, 0.0
    return ws.wait_for_end_price(prev_market, timeout)


def previous_window_move_pct(ws, coin, duration, state):
    """Last window's move in %, or None if unknown; also keeps its $ move on the state for the trades log."""
    prev_open, prev_close = previous_window_prices(ws, coin, duration, state.window_start,
                                                   OFFICIAL_PRICE_WAIT_SECONDS)
    if not prev_open or not prev_close:
        return None
    state.prev_move = prev_close - prev_open
    return (prev_close - prev_open) / prev_open * 100


def decide_window(ui, ws, sim, state, coin, duration, feed_id):
    """Holds the orders until the last window's move is known, then trades or skips (the --max-prev-move-pct filter)."""
    try:
        prev_move = previous_window_move_pct(ws, coin, duration, state)
    except Exception:
        prev_move = None
    if not state.decision_pending:
        ui.prev_move_known(state, prev_move, None)
        return
    reason = sim.prev_move_skip_reason(prev_move)
    sim.resolve_decision(state, reason)
    message = None
    if reason and prev_move is None:
        message = "Last window's move is unknown (no official prices seen): logging this window without trading."
    elif reason:
        message = (f"Last window moved {prev_move:+.3f}% (limit {sim.max_prev_move_pct:.2f}%): "
                   f"logging this window without trading.")
    ui.prev_move_known(state, prev_move, message)


def _compare(start, end):
    if not start or not end:
        return "UNKNOWN"
    return "Up" if end > start else "Down" if end < start else "Tie"


def settle_window(ui, ws, sim, state, feed_id, duration, updown_file, price_to_beat):
    """The result comes only from Predict.fun: the official start/end prices pushed on the socket (~3s after
    the end), else the GetMarket resolution."""
    try:
        official_start, official_end = ws.wait_for_end_price(state.market_id, OFFICIAL_PRICE_WAIT_SECONDS)

        def write_logs(outcome, start_price, end_price, notes, info=None):
            info = info or fetch_market(state.market_id)
            if info:
                state.volume_usd = info["volume_usd"]
            log_to_csv(updown_file, state.window_start, start_price, end_price,
                       format_lowest_cents(state.lowest_up), format_lowest_cents(state.lowest_down), outcome)
            row = sim.settle(
                state, outcome, start_price, end_price, state.prev_move,
                maker_fee_bps=info["maker_fee_bps"] if info else state.maker_fee_bps,
                taker_fee_bps=info["taker_fee_bps"] if info else state.taker_fee_bps,
                notes=notes,
            )
            return (float(row["pnl"]) if row["pnl"] else None), float(row["capital_after"])

        if official_start and official_end:
            # The GetMarket resolution can lag by minutes, so it is only checked a few times afterwards.
            outcome = _compare(official_start, official_end)
            _, pnl = sim.compute_result(state, outcome, state.maker_fee_bps, state.taker_fee_bps)
            ui.window_ended(state, outcome, official_start, official_end, pnl, "official")
            pnl, capital = write_logs(outcome, official_start, official_end, ["official_prices"])
            ui.confirmed(state, outcome, outcome, pnl, capital, "Predict.fun")
            for _ in range(RESOLUTION_CHECK_ATTEMPTS):
                time.sleep(RESOLUTION_CHECK_POLL_SECONDS)
                info = fetch_market(state.market_id)
                if info and info["resolved"]:
                    if info["resolution"] != outcome:
                        ui.event(f"⚠️ {hhmm(state.window_start)} window resolved {info['resolution']} on Predict.fun, "
                                 f"logged as {outcome} from the official prices")
                    return
            return

        info = None
        for _ in range(RESOLUTION_CHECK_ATTEMPTS):
            info = fetch_market(state.market_id) or info
            if info and info["resolved"]:
                break
            time.sleep(RESOLUTION_CHECK_POLL_SECONDS)
        outcome = info["resolution"] if info and info["resolved"] else "UNKNOWN"
        notes = ["resolution_only"] if outcome != "UNKNOWN" else ["no_official_result"]
        _, pnl = sim.compute_result(state, outcome, state.maker_fee_bps, state.taker_fee_bps)
        ui.window_ended(state, outcome, official_start, official_end, pnl, "resolution")
        pnl, capital = write_logs(outcome, official_start, official_end, notes, info)
        ui.confirmed(state, outcome, outcome, pnl, capital, "Predict.fun")
    except Exception as e:
        ui.event(f"⚠️ Settling window {state.window_start} failed: {e}")


def run_window(ui, ws, sim, coin, duration, feed_id, updown_file, price_to_beat):
    """Tracks one window's asks for the simulator, shows its result, and returns the next window's PTB."""
    window_start, window_end = window_bounds(duration)
    slug = get_current_active_slug(coin, duration)

    market_id, category_id, title = get_market_metadata_for_slug(slug)
    if not market_id:
        ui.event(f"⚠️ [{stamp()}] No Predict.fun market for {slug} yet. Retrying in 2 seconds...")
        time.sleep(2)
        return price_to_beat

    ws.set_market(market_id)
    skip_reason = "no_ptb" if price_to_beat <= 0 else None
    state = sim.start_window(window_start, window_end, slug, market_id, skip_reason=skip_reason,
                             decision_pending=skip_reason is None)
    threading.Thread(target=load_fees, args=(state,), daemon=True).start()
    if skip_reason == "no_ptb":
        skip_message = "No Price to Beat given: logging this window without trading."
    elif not state.trading:
        skip_message = f"Joined {int(time.time() - window_start)}s late: logging this window without trading."
    else:
        skip_message = None
    ui.window_started(state, title, slug, market_id, price_to_beat, None, skip_message)
    threading.Thread(target=decide_window, args=(ui, ws, sim, state, coin, duration, feed_id), daemon=True).start()
    next_prepared = False

    while True:
        now = time.time()
        remaining = max(0.0, window_end - now)

        if not next_prepared and remaining <= NEXT_WINDOW_SUBSCRIBE_SECONDS:
            next_prepared = True
            threading.Thread(target=prepare_next_window, args=(ws, coin, duration, window_end), daemon=True).start()

        if now >= window_end:
            state = sim.finish_window()
            ptb = ws.official_prices(market_id)[0] or ws.first_price_from(window_start) or price_to_beat
            ui.settlement_pending(state)
            threading.Thread(
                target=settle_window,
                args=(ui, ws, sim, state, feed_id, duration, updown_file, ptb),
                daemon=True,
            ).start()
            return ws.latest_price() or price_to_beat

        up_ask, down_ask = ws.asks(market_id)
        sim.on_tick(now, up_ask, down_ask)
        if state.depth_start is None and up_ask > 0 and down_ask > 0:
            state.depth_start = ws.depth(market_id)
        if state.depth_first_fill is None and state.fill_type != "none":
            state.depth_first_fill = ws.depth(market_id)

        socket_info = {
            "connected": ws.connected,
            "book_age": ws.book_age(market_id),
            "next_ready": ws.next_ready(),
        }
        ptb = ws.official_prices(market_id)[0] or ws.first_price_from(window_start) or price_to_beat
        ui.tick(state, up_ask, down_ask, ptb, ws.latest_price(), socket_info)
        time.sleep(TICK_SECONDS)


def parse_args():
    parser = argparse.ArgumentParser(description="Predict.fun dual-only up/down simulator")
    parser.add_argument("--coin", default="btc", help="Coin prefix used in the slug, e.g. btc, eth, bnb (default btc).")
    parser.add_argument("-d", "--duration", type=int, choices=SUPPORTED_DURATIONS, default=5,
                        help="Window length in minutes (default 5).")
    parser.add_argument("--limit", type=int, default=30, help="Limit price in cents for both sides (default 30).")
    parser.add_argument("--contracts", type=int, default=8, help="Contracts per side (default 8).")
    parser.add_argument("--place-at", type=int, default=None, metavar="SECONDS_LEFT",
                        help="Place both limits when this many seconds are left (default: at window start).")
    parser.add_argument("--max-prev-move-pct", type=float, default=0.10, metavar="PCT",
                        help="Skip trading a window when the last one moved this many percent or more "
                             "(default 0.10; 0 = off).")
    parser.add_argument("--hedge-at", type=int, default=50, metavar="CENTS",
                        help="After one side fills, split the other side: enough contracts at this price to break "
                             "even if it wins, the rest at --limit (default 50; 0 = off).")
    parser.add_argument("--cancel-left-pct", type=float, default=40.0,
                        help="Cancel both orders when nothing has filled and this much of the window is left, "
                             "in percent (default 40: 2:00 left of 5m, 6:00 of 15m, 24:00 of 60m; 0 = never).")
    parser.add_argument("--capital", type=float, default=100.0,
                        help="Starting balance when there is no trades log yet (default 100).")
    parser.add_argument("--capital-last", type=float, default=None, metavar="EQUITY",
                        help="Restore the starting balance (overrides the last capital_after in the log).")
    parser.add_argument("--feed-id", type=int, default=None,
                        help="Chainlink feed id for the coin (default from the built-in map: btc 1, eth 2, bnb 3).")
    parser.add_argument("--plain", action="store_true",
                        help="Plain line output instead of the live dashboard (for nohup / log files).")
    return parser.parse_args()


def main():
    args = parse_args()
    coin = args.coin.strip().lower()
    duration = args.duration
    feed_id = args.feed_id or CHAINLINK_FEED_IDS.get(coin)
    if not feed_id:
        raise SystemExit(f"No Chainlink feed id known for {coin!r}; pass --feed-id (run discover.py to find it).")
    if not 1 <= args.limit <= 99:
        raise SystemExit("--limit must be between 1 and 99 cents")
    if args.contracts <= 0:
        raise SystemExit("--contracts must be > 0")
    if args.place_at is not None and not 0 < args.place_at <= duration * 60:
        raise SystemExit(f"--place-at must be between 1 and {duration * 60} seconds")
    if args.max_prev_move_pct < 0:
        raise SystemExit("--max-prev-move-pct must be >= 0")
    if args.hedge_at and not args.limit < args.hedge_at < 100:
        raise SystemExit("--hedge-at must be above --limit and below 100 (or 0 for off)")
    if not 0 <= args.cancel_left_pct < 100:
        raise SystemExit("--cancel-left-pct must be between 0 and 100")

    updown_file = f"{coin}-{duration}-updown.csv"
    trades_file = f"{coin}-{duration}-predict-dual-trades.csv"
    if args.capital_last is not None:
        if args.capital_last <= 0:
            raise SystemExit("--capital-last must be > 0")
        capital, source = args.capital_last, "cli"
    else:
        capital, source = last_capital_after(trades_file, args.capital)

    sim = DualSim(coin, duration, args.limit, args.contracts, args.place_at, trades_file, capital, source,
                  max_prev_move_pct=args.max_prev_move_pct, cancel_left_pct=args.cancel_left_pct, hedge_cents=args.hedge_at)
    print(f"⚙️  Predict.fun dual | coin={coin} | duration={duration}m | Chainlink feed {feed_id}")
    print(sim.summarize())
    print(f"📁 updown={updown_file} | trades={trades_file}")

    print("🔌 Initializing Predict.fun socket...")
    ws = PredictWS(feed_id)

    while True:
        try:
            val = input("\nEnter initial Price to Beat (e.g. 82741.02, blank = skip trading the first window): ").strip()
        except EOFError:
            val = ""
        if not val:
            current_ptb = 0.0
            break
        try:
            current_ptb = float(val)
            break
        except ValueError:
            print("❌ Invalid number format. Please enter a valid decimal price (e.g. 82741.02).")

    ui = PlainUI(sim, coin, duration) if args.plain else Dashboard(sim, coin, duration)
    ui.start()
    try:
        while True:
            try:
                current_ptb = run_window(ui, ws, sim, coin, duration, feed_id, updown_file, current_ptb)
            except Exception as e:
                ui.event(f"Error in loop execution: {e}. Restarting cycle...")
                time.sleep(2)
    finally:
        ui.stop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Stopped.")
