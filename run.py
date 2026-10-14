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

from combo import ComboSim
from dual import DualSim, last_capital_after, last_reloaded_total
from leader import LeaderSim
from logs import format_lowest_cents, format_time, log_to_csv
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
EARLY_VOLUME_PCT = 20
VOLUME_POLL_SECONDS = 15
PREOPEN_SAMPLE_SECONDS = 10
preopen_asks = {}  # window_start -> (up, down) of that market sampled PREOPEN_SAMPLE_SECONDS before it opened
PREOPEN_SAMPLE_SECONDS = 10
preopen_asks = {}  # window_start -> (up, down) of that market sampled PREOPEN_SAMPLE_SECONDS before it opened


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


def load_early_volume(state, remaining):
    info = fetch_market(state.market_id)
    if info:
        state.volume_early_usd = info["volume_usd"]
        state.volume_early_time_left = remaining
        state.volume_live = info["volume_usd"]


def load_live_volume(state):
    info = fetch_market(state.market_id)
    if info:
        state.volume_live = info["volume_usd"]


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
    if sim.strategy in ("regime", "fav", "against", "combo"):
        decide_regime(ui, sim, state, prev_move)
        return
    reason = sim.prev_move_skip_reason(prev_move)
    sim.resolve_decision(state, reason)
    message = None
    if reason and prev_move is None:
        message = "⏸️ Last window's move is unknown (no official prices seen): logging this window without trading."
    elif reason and sim.strategy == "leader":
        message = (f"⏸️ Last window moved {prev_move:+.3f}% (needs {sim.min_prev_move_pct:.2f}%+): "
                   f"logging this window without trading.")
    elif reason:
        message = (f"⏸️ Last window moved {prev_move:+.3f}% (limit {sim.max_prev_move_pct:.2f}%): "
                   f"logging this window without trading.")
    elif sim.strategy == "leader":
        start, end = sim.watch_seconds()
        message = (f"👀 Last window moved {prev_move:+.3f}%: watching {format_time(start)}-{format_time(end)} in "
                   f"for a side to reach {sim.leader_at_cents}¢, then buying it at its ask (≤{sim.leader_max_cents}¢).")
    ui.prev_move_known(state, prev_move, message)


def decide_regime(ui, sim, state, prev_move):
    """Dual, favourite or skip from the recent windows' winners and duals (the regime strategy)."""
    prev_outcome = None
    if state.prev_move is not None and prev_move is not None:
        prev_outcome = "Up" if state.prev_move > 0 else "Down" if state.prev_move < 0 else "Tie"
    action, reason = sim.regime_decision(state.window_start, prev_outcome, prev_move)
    state.action, state.action_reason = action, reason
    if not state.trading:
        sim.resolve_decision(state)
        ui.prev_move_known(state, prev_move, None)
        return
    sim.resolve_decision(state, reason if action == "skip" else None,
                         mode=action if action in ("fav", "against") else "dual")
    if sim.strategy in ("against", "combo"):
        moved = f"{prev_move:+.3f}%" if prev_move is not None else "unknown"
        streak = f"streak {state.streak_before}, last window moved {moved}"
    else:
        streak = f"streak {state.streak_before}, duals in last {sim.dual_lookback}: {state.recent_duals}"
    if action == "against":
        message = (f"🎯 Against-the-streak window ({streak}): buying {sim.contracts} {state.bet_side} at its ask "
                   f"if ≤{sim.against_max_cents}¢, held to the end.")
    elif action == "fav":
        message = (f"🎯 Favourite window ({streak}): buying {sim.contracts} of the higher-priced side at its ask "
                   f"if ≤{sim.fav_max_cents}¢, held to the end.")
    elif action == "dual":
        message = f"🎯 Dual window ({streak}): placing both {sim.limit_cents}¢ limits."
    else:
        message = f"⏸️ Skipping ({reason}; {streak}): logging this window without trading."
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
    state.preopen_asks = preopen_asks.pop(window_start, None)
    threading.Thread(target=load_fees, args=(state,), daemon=True).start()
    if skip_reason == "no_ptb":
        skip_message = "No Price to Beat given: logging this window without trading."
    elif not state.trading:
        skip_message = f"Joined {int(time.time() - window_start)}s late: logging this window without trading."
    else:
        skip_message = None
    ui.window_started(state, title, slug, market_id, price_to_beat, None, skip_message)
    if state.reload:
        ui.event(f"💰 Balance too low for both orders: reloaded +${state.reload:.2f} to ${sim.capital:.2f} "
                 f"(${sim.reloaded_total:.2f} reloaded in total)")
    threading.Thread(target=decide_window, args=(ui, ws, sim, state, coin, duration, feed_id), daemon=True).start()
    next_prepared = False
    early_volume_asked = False
    last_volume_poll = 0.0
    preopen_sampled = False

    while True:
        now = time.time()
        remaining = max(0.0, window_end - now)

        if not next_prepared and remaining <= NEXT_WINDOW_SUBSCRIBE_SECONDS:
            next_prepared = True
            threading.Thread(target=prepare_next_window, args=(ws, coin, duration, window_end), daemon=True).start()

        if not preopen_sampled and remaining <= PREOPEN_SAMPLE_SECONDS:
            next_id = ws.next_market()
            if next_id:
                next_up, next_down = ws.asks(next_id)
                if next_up > 0 and next_down > 0:
                    preopen_asks[window_end] = (next_up, next_down)
                    preopen_sampled = True

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
        ptb = ws.official_prices(market_id)[0] or ws.first_price_from(window_start) or price_to_beat
        btc = ws.latest_price()
        sim.on_tick(now, up_ask, down_ask, btc or 0.0, ptb or 0.0)
        if state.depth_start is None and up_ask > 0 and down_ask > 0:
            state.depth_start = ws.depth(market_id)
        if state.depth_first_fill is None and state.fill_type != "none":
            state.depth_first_fill = ws.depth(market_id)
        state.record_book(ws.depth(market_id), up_ask, down_ask)
        state.book_now = ws.levels(market_id)
        if not early_volume_asked and remaining <= (window_end - window_start) * (1 - EARLY_VOLUME_PCT / 100):
            early_volume_asked = True
            threading.Thread(target=load_early_volume, args=(state, remaining), daemon=True).start()
        elif now - last_volume_poll >= VOLUME_POLL_SECONDS:
            last_volume_poll = now
            threading.Thread(target=load_live_volume, args=(state,), daemon=True).start()

        socket_info = {
            "connected": ws.connected,
            "book_age": ws.book_age(market_id),
            "next_ready": ws.next_ready(),
        }
        ui.tick(state, up_ask, down_ask, ptb, btc, socket_info)
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
    parser.add_argument("--leader-at", type=int, default=60, metavar="CENTS",
                        help="Leader: a side is the leader when its bid first reaches this in the watch period "
                             "(default 60).")
    parser.add_argument("--leader-max", type=int, default=70, metavar="CENTS",
                        help="Leader: buy the leader at its ask only up to this price (default 70).")
    parser.add_argument("--min-prev-move-pct", type=float, default=0.10, metavar="PCT",
                        help="Leader: trade only when the last window moved this many percent or more "
                             "(default 0.10; 0 = any move).")
    parser.add_argument("--watch-from-pct", type=float, default=20.0,
                        help="Leader: start of the watch period, in percent of the window (default 20: 1:00 into 5m).")
    parser.add_argument("--watch-to-pct", type=float, default=40.0,
                        help="Leader: end of the watch period, in percent of the window (default 40: 2:00 into 5m).")
    parser.add_argument("--strategy", choices=("leader", "regime", "fav", "against", "combo", "dual"), default=None,
                        help="leader: after a last move of --min-prev-move-pct+, buy the first side to reach "
                             "--leader-at in the watch period at its ask, held to the end (own trades file). "
                             "regime: after exactly 2 same winners in a row buy the favourite, else dual if one of "
                             "the last --dual-lookback windows was a dual, else skip. fav: only the favourite bets, "
                             "no duals. against: only buy the side against a streak of --against-min-streak+ when "
                             "the last window moved --against-min-move%%+. combo: fav and against together. (fav, "
                             "against and combo have their own trades file and can run beside another bot.) dual: the old dual every window with the "
                             "--max-prev-move-pct filter. (default: regime for 5m, dual otherwise)")
    parser.add_argument("--against-min-streak", type=int, default=3, metavar="WINDOWS",
                        help="Against: bet only after this many same winners in a row or more (default 3).")
    parser.add_argument("--against-min-move", type=float, default=0.02, metavar="PCT",
                        help="Against: bet only when the last window moved this many percent or more (default 0.02).")
    parser.add_argument("--against-max", type=int, default=99, metavar="CENTS",
                        help="Against: only buy at this ask or below (default 99 = no cap).")
    parser.add_argument("--fav-max-move", type=float, default=0.02, metavar="PCT",
                        help="Only take the favourite bet when the last window moved less than this many percent "
                             "(default 0.02; 0 = any move).")
    parser.add_argument("--fav-max", type=int, default=55, metavar="CENTS",
                        help="Regime: only buy the favourite at this ask or below (default 55).")
    parser.add_argument("--dual-lookback", type=int, default=3, metavar="WINDOWS",
                        help="Regime: trade the dual only if one of this many last windows was a dual (default 3).")
    parser.add_argument("--max-prev-move-pct", type=float, default=0.10, metavar="PCT",
                        help="Dual strategy: skip trading a window when the last one moved this many percent or more "
                             "(default 0.10; 0 = off).")
    parser.add_argument("--hedge-at", type=int, default=0, metavar="CENTS",
                        help="After one side fills, split the other side: enough contracts at this price to break "
                             "even if it wins, the rest at --limit (default 0 = off; e.g. 50).")
    parser.add_argument("--stop-loss-at", type=int, default=20, metavar="SECONDS_LEFT",
                        help="With this many seconds left, if the window would lose with the side now leading as "
                             "the winner, sell the losing side's unmatched contracts at its bid (default 20; 0 = off).")
    parser.add_argument("--stop-loss-live", action=argparse.BooleanOptionalAction, default=True,
                        help="Actually sell when the stop-loss fires (default). --no-stop-loss-live only logs what "
                             "it would sell (would_stop_* columns).")
    parser.add_argument("--stop-loss-min", type=int, default=3, metavar="CENTS",
                        help="Only sell when the losing side's bid is at least this many cents; below that keep "
                             "the contracts and log it (default 3; 0 = any price).")
    parser.add_argument("--cancel-left-pct", type=float, default=40.0,
                        help="Cancel both orders when nothing has filled and this much of the window is left, "
                             "in percent (default 40: 2:00 left of 5m, 6:00 of 15m, 24:00 of 60m; 0 = never).")
    parser.add_argument("--capital", type=float, default=100.0,
                        help="Starting balance when there is no trades log yet (default 100).")
    parser.add_argument("--reload-to", type=float, default=None, metavar="DOLLARS",
                        help="When the balance can't cover both orders at a window's start, top it back up to this "
                             "(default: --capital; 0 = never). Top-ups are logged in reload / reloaded_total.")
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
    strategy = args.strategy or ("regime" if duration == 5 else "dual")
    if not 1 <= args.fav_max <= 99:
        raise SystemExit("--fav-max must be between 1 and 99 cents")
    if args.dual_lookback < 1:
        raise SystemExit("--dual-lookback must be >= 1")
    if args.against_min_streak < 1 or args.against_min_move < 0 or not 1 <= args.against_max <= 99:
        raise SystemExit("--against-min-streak must be >= 1, --against-min-move >= 0, --against-max 1-99")
    if args.max_prev_move_pct < 0:
        raise SystemExit("--max-prev-move-pct must be >= 0")
    if args.hedge_at and not args.limit < args.hedge_at < 100:
        raise SystemExit("--hedge-at must be above --limit and below 100 (or 0 for off)")
    if not 0 <= args.stop_loss_at < duration * 60:
        raise SystemExit(f"--stop-loss-at must be between 0 and {duration * 60 - 1} seconds")
    if not 0 <= args.cancel_left_pct < 100:
        raise SystemExit("--cancel-left-pct must be between 0 and 100")

    updown_file = f"{coin}-{duration}-updown.csv"
    dual_trades_file = f"{coin}-{duration}-predict-dual-trades.csv"
    if not 1 <= args.leader_at <= args.leader_max <= 99:
        raise SystemExit("--leader-at and --leader-max must be between 1 and 99 cents, --leader-max >= --leader-at")
    if args.min_prev_move_pct < 0:
        raise SystemExit("--min-prev-move-pct must be >= 0")
    if not 0 <= args.watch_from_pct < args.watch_to_pct < 100:
        raise SystemExit("--watch-from-pct must be below --watch-to-pct, both between 0 and 100")
    if strategy in ("leader", "fav", "against", "combo"):
        trades_file = f"{coin}-{duration}-predict-{strategy}-trades.csv"
    else:
        trades_file = dual_trades_file
    if args.capital_last is not None:
        if args.capital_last <= 0:
            raise SystemExit("--capital-last must be > 0")
        capital, source = args.capital_last, "cli"
    else:
        capital, source = last_capital_after(trades_file, args.capital)

    sim_args = (coin, duration, args.limit, args.contracts, args.place_at, trades_file, capital, source)
    common = dict(max_prev_move_pct=args.max_prev_move_pct, cancel_left_pct=args.cancel_left_pct,
                  hedge_cents=args.hedge_at, stop_loss_seconds=args.stop_loss_at, stop_loss_live=args.stop_loss_live,
                  stop_loss_min_cents=args.stop_loss_min,
                  reload_to=args.capital if args.reload_to is None else args.reload_to,
                  reloaded_total=last_reloaded_total(trades_file))
    if strategy == "leader":
        sim = LeaderSim(*sim_args, **common, leader_at_cents=args.leader_at, leader_max_cents=args.leader_max,
                        min_prev_move_pct=args.min_prev_move_pct, watch_from_pct=args.watch_from_pct,
                        watch_to_pct=args.watch_to_pct)
    elif strategy == "dual":
        sim = DualSim(*sim_args, **common)
    else:
        sim = ComboSim(*sim_args, **common, strategy=strategy, fav_max_cents=args.fav_max,
                       dual_lookback=args.dual_lookback, against_min_streak=args.against_min_streak,
                       against_min_move_pct=args.against_min_move, against_max_cents=args.against_max,
                       fav_max_move_pct=args.fav_max_move)
        if trades_file != dual_trades_file:
            sim.load_history(dual_trades_file)
        sim.load_history(trades_file)
    print(f"⚙️  Predict.fun {strategy} | coin={coin} | duration={duration}m | Chainlink feed {feed_id}")
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
