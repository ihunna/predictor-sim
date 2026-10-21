#!/usr/bin/env python3
"""Predict.fun dual-only simulator: a limit on both Up and Down in every window of one coin/duration.

Usage:
    python run.py --coin btc -d 5                # live dashboard (needs a terminal; use tmux/screen on a server)
    python run.py --coin btc -d 5 --plain        # plain lines, for nohup / log files
    python run.py --coin eth -d 15 --limit 30 --contracts 8 --max-prev-move-pct 0.10 --capital-last 112.40

Requires PREDICT_API_KEY in .env (from https://developers.predict.fun).
Rate limit: 240 requests/minute. WebSocket is direct.
"""
import argparse
import os
import socket
import threading
import time

from logs import format_lowest_cents, format_time, log_to_csv
from sim import last_capital_after, last_reloaded_total
from strategies import AgainstSim, ComboSim, DualSim, LeaderSim, ScalpSim, STRATEGIES
from predict_feed import (
    API_RATE_PER_MINUTE,
    CHAINLINK_FEED_IDS,
    SUPPORTED_DURATIONS,
    PredictWS,
    api_base_url,
    api_configured,
    configure_api_from_env,
    fetch_market,
    get_current_active_slug,
    get_last_lookup_error,
    get_market_metadata_for_slug,
    load_dotenv,
    set_api_key,
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
# 5m usually gets official end in ~3s; hourly (60m) often sits in PRICE_PROPOSED for minutes.
OFFICIAL_PRICE_WAIT_SECONDS = {5: 10, 15: 30, 60: 120}
LEADER_MIN_PREV_MOVE_PCT = {5: 0.10, 15: 0.15, 60: 0.35}  # about the biggest 30% of window moves
# combo / fav move thresholds are set on 5m and scaled by how much bigger longer windows move (against: not scaled)
STREAK_MOVE_PCT_5M = {"against_min_move": 0.02, "continue_max_move": 0.05, "fav_max_move": 0.02,
                      "trend_against_min_move": 0.05}
MOVE_SCALE = {5: 1.0, 15: 1.5, 60: 3.5}
# dual: watch from open; |Δ| gate for cheap+high signal (scaled by duration)
DUAL_ENTER_AFTER_PCT = 0.0
DUAL_QUIET_MAX_PCT_5M = 0.01

# (attempts, poll_seconds): ~1m on 5m, ~2m on 15m, ~10m on 60m for GetMarket resolution fallback
RESOLUTION_CHECK = {5: (4, 15), 15: (8, 15), 60: (30, 20)}
EARLY_VOLUME_PCT = 20
VOLUME_POLL_SECONDS = 15
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
    wait = OFFICIAL_PRICE_WAIT_SECONDS.get(duration, 10)
    prev_open, prev_close = previous_window_prices(ws, coin, duration, state.window_start, wait)
    if not prev_open or not prev_close:
        return None
    state.prev_move = prev_close - prev_open
    return (prev_close - prev_open) / prev_open * 100


def _fill_structure(sim, state, prev_move):
    """Populate structure/RSI/move-vs-avg on every strategy for the UI and trade log."""
    avg, vs_avg, rsi, structure = sim.refresh_structure(state.window_start, prev_move_pct=prev_move)
    state.avg_move_pct, state.move_vs_avg, state.rsi, state.structure = avg, vs_avg, rsi, structure or ""


def decide_window(ui, ws, sim, state, coin, duration, feed_id):
    """Holds the orders until the last window's move is known, then trades or skips (the --max-prev-move-pct filter)."""
    try:
        prev_move = previous_window_move_pct(ws, coin, duration, state)
    except Exception:
        prev_move = None
    if not state.decision_pending:
        # Still refresh so skipped / late-join windows log structure + RSI.
        if sim.strategy not in ("against", "dual", "scalp"):
            _fill_structure(sim, state, prev_move)
        ui.prev_move_known(state, prev_move, None)
        return
    if sim.strategy in ("regime", "fav", "against", "combo"):
        # against refreshes inside regime_decision (with streak_len); others need it here.
        if sim.strategy != "against":
            _fill_structure(sim, state, prev_move)
        decide_regime(ui, sim, state, prev_move)
        return
    if sim.strategy == "dual":
        decide_dual(ui, sim, state, prev_move)
        return
    if sim.strategy == "scalp":
        decide_scalp(ui, sim, state, prev_move)
        return
    _fill_structure(sim, state, prev_move)
    reason = sim.prev_move_skip_reason(prev_move)
    sim.resolve_decision(state, reason)
    message = None
    if reason and prev_move is None:
        message = "[skip] Last window's move is unknown (no official prices seen): logging this window without trading."
    elif reason and sim.strategy == "leader":
        message = (f"[skip] Last window moved {prev_move:+.3f}% (needs {sim.min_prev_move_pct:.2f}%+): "
                   f"logging this window without trading.")
    elif reason:
        message = (f"[skip] Last window moved {prev_move:+.3f}% (limit {sim.max_prev_move_pct:.2f}%): "
                   f"logging this window without trading.")
    elif sim.strategy == "leader":
        start, end = sim.watch_seconds()
        message = (f"[watch] Last window moved {prev_move:+.3f}%: watching {format_time(start)}-{format_time(end)} in "
                   f"for a side to reach {sim.leader_at_cents}¢, then buying it at its ask (≤{sim.leader_max_cents}¢).")
    ui.prev_move_known(state, prev_move, message)


def _structure_gate_reason(sim, state, prev_move):
    """Refresh structure/RSI and return skip reason if require_structure is not met (dual + scalp)."""
    avg, vs_avg, rsi, structure = sim.refresh_structure(state.window_start, prev_move_pct=prev_move)
    state.avg_move_pct, state.move_vs_avg, state.rsi, state.structure = avg, vs_avg, rsi, structure or ""
    reason = sim.prev_move_skip_reason(prev_move)
    need = (getattr(sim, "require_structure", "") or "").strip().lower()
    if not reason and need and (structure or "").lower() != need:
        reason = f"structure_{structure or 'unknown'}_need_{need}"
    struct = structure or "unknown"
    streak = f"structure {struct}"
    if rsi is not None:
        streak += f", RSI {rsi:.0f}"
    if vs_avg is not None:
        streak += f", move {vs_avg:.2f}× avg"
    return reason, streak


def decide_dual(ui, sim, state, prev_move):
    """Refresh structure/RSI (always logged); optional require_structure skip; else place both limits."""
    reason, streak = _structure_gate_reason(sim, state, prev_move)
    sim.resolve_decision(state, reason)
    if reason:
        message = f"[skip] Skipping dual ({reason}; {streak}): logging this window without trading."
    else:
        message = (f"Dual window ({streak}): placing both {sim.limit_cents}¢ x{sim.contracts} limits.")
    ui.prev_move_known(state, prev_move, message)


def decide_scalp(ui, sim, state, prev_move):
    """Refresh structure/RSI (always logged); optional require_structure skip; else watch ask≤entry."""
    reason, streak = _structure_gate_reason(sim, state, prev_move)
    sim.resolve_decision(state, reason)
    tp = sim.tp_text() if hasattr(sim, "tp_text") else (
        f"TP {sim.target_cents}¢" if getattr(sim, "target_cents", 0) > 0 else "hold"
    )
    if reason:
        message = f"[skip] Skipping scalp ({reason}; {streak}): logging this window without trading."
    else:
        message = (f"Scalp window ({streak}): buy ask≤{sim.entry_cents}¢ x{sim.contracts}, {tp}.")
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
                         mode=action if action in ("fav", "against", "continue") else "dual")
    if sim.strategy in ("against", "combo"):
        moved = f"{prev_move:+.3f}%" if prev_move is not None else "unknown"
        streak = f"streak {state.streak_before}, last window moved {moved}"
        if sim.strategy == "against":
            streak += f", structure {state.structure or getattr(sim, 'structure', None) or 'unknown'}"
            if state.rsi is not None:
                streak += f", RSI {state.rsi:.0f}"
    else:
        streak = f"streak {state.streak_before}, duals in last {sim.dual_lookback}: {state.recent_duals}"
    if action == "against":
        held = (f"sold at {round(state.take_profit * 100)}¢ (take-profit) or held to the end" if state.take_profit
                else "held to the end")
        message = (f"Against-the-streak window ({streak}): buying {sim.contracts} {state.bet_side} at its ask "
                   f"if ≤{sim.against_max_cents}¢, {held}.")
    elif action == "continue":
        message = (f"Streak-continues window ({streak}, slowing): buying {sim.contracts} {state.bet_side} at its "
                   f"ask if ≤{sim.against_max_cents}¢, held to the end.")
    elif action == "fav":
        message = (f"Favourite window ({streak}): buying {sim.contracts} of the higher-priced side at its ask "
                   f"if ≤{sim.fav_max_cents}¢, held to the end.")
    elif action == "dual":
        message = f"Dual window ({streak}): placing both {sim.limit_cents}¢ limits."
    else:
        message = f"[skip] Skipping ({reason}; {streak}): logging this window without trading."
    ui.prev_move_known(state, prev_move, message)


def _compare(start, end):
    if not start or not end:
        return "UNKNOWN"
    return "Up" if end > start else "Down" if end < start else "Tie"


def _inferred_prices(ws, state, price_to_beat):
    """Feed stand-in for (open, close) when Predict.fun has not pushed official prices yet."""
    start = price_to_beat or ws.first_price_from(state.window_start) or 0.0
    end = ws.price_before(state.window_end) or ws.latest_price() or 0.0
    return start, end


def settle_window(ui, ws, sim, state, feed_id, duration, updown_file, price_to_beat):
    """Prefer Predict.fun official start/end, else infer Up/Down from the live feed (same compare),
    else wait on GetMarket resolution. Without a result, capital stays unsettled."""
    try:
        price_wait = OFFICIAL_PRICE_WAIT_SECONDS.get(duration, 10)
        res_attempts, res_poll = RESOLUTION_CHECK.get(duration, (4, 15))
        official_start, official_end = ws.wait_for_end_price(state.market_id, price_wait)

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

        def verify_resolution(logged_outcome, logged_source):
            for _ in range(res_attempts):
                time.sleep(res_poll)
                info = fetch_market(state.market_id)
                if info and info["resolved"]:
                    if info["resolution"] != logged_outcome:
                        ui.event(f"[!] {hhmm(state.window_start)} window resolved {info['resolution']} on Predict.fun, "
                                 f"logged as {logged_outcome} from {logged_source}")
                    return

        if official_start and official_end:
            outcome = _compare(official_start, official_end)
            _, pnl = sim.compute_result(state, outcome, state.maker_fee_bps, state.taker_fee_bps)
            ui.window_ended(state, outcome, official_start, official_end, pnl, "official")
            pnl, capital = write_logs(outcome, official_start, official_end, ["official_prices"])
            ui.confirmed(state, outcome, outcome, pnl, capital, "Predict.fun")
            verify_resolution(outcome, "the official prices")
            return

        inferred_start, inferred_end = _inferred_prices(ws, state, price_to_beat)
        inferred_outcome = _compare(inferred_start, inferred_end)
        if inferred_outcome != "UNKNOWN":
            _, pnl = sim.compute_result(state, inferred_outcome, state.maker_fee_bps, state.taker_fee_bps)
            ui.window_ended(state, inferred_outcome, inferred_start, inferred_end, pnl, "inferred")
            pnl, capital = write_logs(inferred_outcome, inferred_start, inferred_end, ["inferred_prices"])
            ui.confirmed(state, inferred_outcome, inferred_outcome, pnl, capital, "feed inference")
            verify_resolution(inferred_outcome, "feed inference")
            return

        info = None
        for _ in range(res_attempts):
            info = fetch_market(state.market_id) or info
            if info and info["resolved"]:
                break
            time.sleep(res_poll)
        outcome = info["resolution"] if info and info["resolved"] else "UNKNOWN"
        start = official_start or inferred_start
        end = official_end or inferred_end
        notes = ["resolution_only"] if outcome != "UNKNOWN" else ["no_official_result"]
        _, pnl = sim.compute_result(state, outcome, state.maker_fee_bps, state.taker_fee_bps)
        ui.window_ended(state, outcome, start, end, pnl, "resolution")
        pnl, capital = write_logs(outcome, start, end, notes, info)
        ui.confirmed(state, outcome, outcome, pnl, capital, "Predict.fun")
    except Exception as e:
        ui.event(f"[!] Settling window {state.window_start} failed: {e}")


def run_window(ui, ws, sim, coin, duration, feed_id, updown_file, price_to_beat):
    """Tracks one window's asks for the simulator, shows its result, and returns the next window's PTB."""
    window_start, window_end = window_bounds(duration)
    slug = get_current_active_slug(coin, duration)

    market_id, category_id, title = get_market_metadata_for_slug(slug)
    if not market_id:
        err = get_last_lookup_error()
        if err:
            ui.event(f"[!] [{stamp()}] Predict lookup failed for {slug}: {err}. Retrying in 2s...")
        else:
            ui.event(f"[!] [{stamp()}] No Predict.fun market for {slug} yet. Retrying in 2 seconds...")
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
        ui.event(f"[reload] Balance too low for both orders: reloaded +${state.reload:.2f} to ${sim.capital:.2f} "
                 f"(${sim.reloaded_total:.2f} reloaded in total)")
    threading.Thread(target=decide_window, args=(ui, ws, sim, state, coin, duration, feed_id), daemon=True).start()
    next_prepared = False
    early_volume_asked = False
    last_volume_poll = 0.0
    preopen_sampled = False
    saw_cancel = state.cancelled_time_left is not None

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
        # Announce cancel as soon as it fires (all strats that use cancel_left_pct).
        if not saw_cancel and state.cancelled_time_left is not None:
            saw_cancel = True
            ui.event(
                f"[cancel] {hhmm(state.window_start)} cancelled no-fill at "
                f"{format_time(state.cancelled_time_left)} left"
            )
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
    parser.add_argument("--min-prev-move-pct", type=float, default=None, metavar="PCT",
                        help="Leader: trade only when the last window moved this many percent or more "
                             "(default by window: 5m 0.10, 15m 0.15, 60m 0.35 - about the biggest 30%% of moves; "
                             "0 = any move).")
    parser.add_argument("--watch-from-pct", type=float, default=20.0,
                        help="Leader: start of the watch period, in percent of the window (default 20: 1:00 into 5m).")
    parser.add_argument("--watch-to-pct", type=float, default=40.0,
                        help="Leader: end of the watch period, in percent of the window (default 40: 2:00 into 5m).")
    parser.add_argument("--strategy",
                        choices=("leader", "regime", "fav", "against", "combo", "dual", "scalp"), default=None,
                        help="leader / regime / fav / against / combo: see prior docs. "
                             "dual: both @--limit (default 30¢); no structure gate by default "
                             "(no quiet/skew/enter-after). "
                             "scalp: split buy ½ ask≤--scalp-entry → TP --scalp-target (default 30→60), "
                             "½ ask≤--scalp-split-entry → TP --scalp-split-target (default 20→40), "
                             "adverse-delta cap per duration (see --scalp-delta-cap). "
                             "Optional --dual-structure to re-gate dual/scalp. "
                             "(default: regime for 5m, dual otherwise)")

    parser.add_argument("--against-filter", choices=("struct_rsi", "struct", "bare", "rsi"), default="struct_rsi",
                        help="Against only. struct_rsi (default): against clear structure + RSI agree with structure. "
                             "struct: against clear structure only (no RSI gate; own trades file). "
                             "bare: every exact streak, skip only not-broken (no struct/RSI gate; own trades file). "
                             "rsi: no structure gate; only fade when the bet agrees with RSI as trend "
                             "(Up if RSI>55, Down if RSI<45; mid/unknown skip; own trades file).")
    parser.add_argument("--against-min-streak", type=int, default=3, metavar="WINDOWS",
                        help="Against: bet at exactly this many same winners in a row; combo: this many or more "
                             "(default 3).")
    parser.add_argument("--against-min-move", type=float, default=None, metavar="PCT",
                        help="Combo: bet only when the last window moved this many percent or more "
                             "(default 0.02 on 5m, 0.03 on 15m, 0.07 on 60m; the combo move defaults below scale "
                             "the same way). Against ignores this and uses --move-vs-avg instead.")
    parser.add_argument("--against-max", type=int, default=99, metavar="CENTS",
                        help="Against and continue: only buy at this ask or below (default 99 = no cap).")
    parser.add_argument("--move-vs-avg", type=float, default=0.0, metavar="TIMES",
                        help="Against: bet only when the last window's move is at least this many times the average "
                             "move of the last --avg-windows windows (default 0 = off; try 1 later to re-enable).")
    parser.add_argument("--avg-windows", type=int, default=24, metavar="WINDOWS",
                        help="Against: windows for the average move and the structure (default 24).")
    parser.add_argument("--take-profit-at", type=int, default=85, metavar="CENTS",
                        help="Against: take-profit sell price when --take-profit-all is set (default 85; 0 = off).")
    parser.add_argument("--take-profit-all", action="store_true",
                        help="Against: put the take-profit on every bet (default: hold to the end).")
    parser.add_argument("--continue-max-move", type=float, default=None, metavar="PCT",
                        help="Combo: after --against-min-streak+ in a row, buy the streak's side when the last window "
                             "moved less than this many percent and less than the window before (default 0.05 on 5m; "
                             "0 = off).")
    parser.add_argument("--trend-windows", type=int, default=None, metavar="WINDOWS",
                        help="Combo: the trend is the side with more 3+ streaks in this many windows before the streak "
                             "(default 8h: 96 on 5m, 32 on 15m, 24 on 60m; 0 = off). A streak against the trend is bet "
                             "to break at --trend-against-min-move%%+, a streak with the trend is bet to continue under "
                             "--continue-max-move%% when slowing, no trend → skip.")
    parser.add_argument("--trend-against-min-move", type=float, default=None, metavar="PCT",
                        help="(default 0.05 on 5m)")
    parser.add_argument("--against-keep-betting", action="store_true",
                        help="Combo: keep betting against a streak after a loss on it (default: once per streak).")
    parser.add_argument("--fav-max-move", type=float, default=None, metavar="PCT",
                        help="Only take the favourite bet when the last window moved less than this many percent "
                             "(default 0.02 on 5m; 0 = any move).")
    parser.add_argument("--fav-max", type=int, default=55, metavar="CENTS",
                        help="Regime: only buy the favourite at this ask or below (default 55).")
    parser.add_argument("--dual-lookback", type=int, default=3, metavar="WINDOWS",
                        help="Regime: trade the dual only if one of this many last windows was a dual (default 3).")
    parser.add_argument("--max-prev-move-pct", type=float, default=0.0, metavar="PCT",
                        help="Dual strategy: skip trading a window when the last one moved this many percent or more "
                             "(default 0 = off; try 0.10 to skip after wild prior windows).")

    parser.add_argument("--enter-after", type=int, default=None, metavar="SECONDS_IN",
                        help="Dual: earliest seconds into the window to place "
                             "(default 0 = from open; legacy, unused when structure gate alone).")
    parser.add_argument("--quiet-max-pct", type=float, default=None, metavar="PCT",
                        help="Dual: max |BTC Δ from PTB| (default 0 = off; dual ignores Δ unless set).")

    parser.add_argument("--cheap-ask", type=int, default=0, metavar="CENTS",
                        help="Dual: 0 = both @--limit only (default). >0 enables legacy skew lift+complement.")
    parser.add_argument("--pair-cents", type=int, default=70, metavar="CENTS",
                        help="Dual skew only: target pair sum for cheap+other (default 70).")
    parser.add_argument("--strand-exit", type=int, default=0, metavar="SECONDS_LEFT",
                        help="Dual: if only one side filled, sell it at bid with this many seconds left "
                             "(default 0 = hold stranded).")
    parser.add_argument("--balanced-dual", action="store_true",
                        help="Dual skew: if cheap never prints, also rest both @--limit (default dual already "
                             "rests both when structure is mixed).")
    parser.add_argument("--dual-structure", default="", metavar="NAME",
                        help="Dual and scalp: only trade when PriceStructure equals this "
                             "(default empty = any / ungated to gather data; set mixed|up|down to gate).")

    parser.add_argument("--scalp-entry", type=int, default=30, metavar="CENTS",
                        help="Scalp: buy when a side's ask is at or below this (default 30; 5m replay).")
    parser.add_argument("--scalp-target", type=int, default=60, metavar="CENTS",
                        help="Scalp: final take-profit when bid reaches this (default 60; 0 = hold to settle; "
                             "miss without TP = −entry at settle).")
    parser.add_argument("--scalp-ladder-first", type=int, default=0, metavar="CENTS",
                        help="Scalp (legacy, split off): sell half the position when bid reaches this "
                             "first ladder level, the rest at --scalp-target (default 0 = off; "
                             "all-in at target).")
    parser.add_argument("--scalp-split-entry", type=int, default=20, metavar="CENTS",
                        help="Scalp: buy the second half only when the ask falls to this "
                             "(default 20; 0 = off, single all-in entry).")
    parser.add_argument("--scalp-split-target", type=int, default=40, metavar="CENTS",
                        help="Scalp: take-profit for the second (deep) half (default 40).")
    parser.add_argument("--scalp-delta-cap", type=float, default=None, metavar="USD",
                        help="Scalp: skip entries when this window's BTC move against the bought "
                             "side already exceeds this many USD at entry. Default: auto by "
                             "duration (5m $20, 15m $50, 60m $200); 0 = off.")
    parser.add_argument("--scalp-exit-left", type=int, default=0, metavar="SECONDS_LEFT",
                        help="Scalp: flatten at bid with this many seconds left if no TP (default 0 = off).")
    parser.add_argument("--scalp-stop", type=int, default=0, metavar="CENTS",
                        help="Scalp: hard stop only if bid stays ≤ this for --scalp-stop-confirm seconds "
                             "(default 0 = off).")
    parser.add_argument("--scalp-stop-confirm", type=int, default=5, metavar="SECONDS",
                        help="Scalp: bid must stay ≤ stop this long before stopping (default 5; filters wicks).")
    parser.add_argument("--rsi-zone", default="", metavar="LO-HI",
                        help="Scalp only: enter only when RSI is in this inclusive band, e.g. 70-100. "
                             "Empty (default) = off. Unknown RSI skips when set.")

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
    parser.add_argument("--cancel-left-pct", type=float, default=67.0,
                        help="Cancel unfilled / block new scalp entries when this much of the window is left, "
                             "in percent (default 67 ≈ first third only: ~3:20 left of 5m, ~10:00 of 15m, "
                             "~40:00 of 60m; 0 = never). From 60m scalp replay: late fills rarely hit TP.")
    parser.add_argument("--savior-left-pct", type=float, default=0.0,
                        help="On one-sided holds, arm savior when this much of the window is left "
                             "(default 0 = off). E.g. 50 ≈ half window left. Locks a cut from winner strength; "
                             "stall-sell after --savior-stall-pct with no progress, or force at "
                             "--savior-force-left-pct. All strategies.")
    parser.add_argument("--savior-stall-pct", type=float, default=10.0,
                        help="After arm, seconds of no new high bid before stall-sell, as %% of window "
                             "(default 10). 0 = never stall (force only). Ignored if savior off.")
    parser.add_argument("--savior-force-left-pct", type=float, default=20.0,
                        help="Force flatten when this much of the window is left if still holding "
                             "(default 20). Ignored if savior off.")
    parser.add_argument("--capital", type=float, default=100.0,
                        help="Starting balance when there is no trades log yet (default 100).")
    parser.add_argument("--reload-to", type=float, default=None, metavar="DOLLARS",
                        help="When the balance can't cover both orders at a window's start, top it back up to this "
                             "(default: --capital; 0 = never). Top-ups are logged in reload / reloaded_total.")
    parser.add_argument("--capital-last", type=float, default=None, metavar="EQUITY",
                        help="Restore the starting balance (overrides the last capital_after in the log).")
    parser.add_argument("--feed-id", type=int, default=None,
                        help="Chainlink feed id for the coin (default from the built-in map: btc 1, eth 2, bnb 3).")
    parser.add_argument("--api-key", default=None, metavar="KEY",
                        help="Predict official REST API key (x-api-key). Overrides .env PREDICT_API_KEY. "
                             "Get one at https://developers.predict.fun.")
    parser.add_argument("--api-testnet", action="store_true",
                        help="Use https://api-testnet.predict.fun (no API key required).")
    parser.add_argument("--plain", action="store_true",
                        help="Plain line output instead of the live dashboard (for nohup / log files).")
    return parser.parse_args()


def main():
    args = parse_args()
    load_dotenv()
    if args.api_key is not None or args.api_testnet:
        key = args.api_key
        if key is None:
            key = os.environ.get("PREDICT_API_KEY") or os.environ.get("API_KEY")
        set_api_key(key, testnet=args.api_testnet)
    else:
        configure_api_from_env()
    if not api_configured():
        raise SystemExit(
            "PREDICT_API_KEY required (set in .env or pass --api-key). "
            "Get one at https://developers.predict.fun — or use --api-testnet."
        )
    print(f"Predict API: {api_base_url()} · ≤{API_RATE_PER_MINUTE}/min · WS direct", flush=True)
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
    scale = 1.0 if strategy == "against" else MOVE_SCALE.get(duration, (duration / 5) ** 0.5)
    for name, pct in STREAK_MOVE_PCT_5M.items():
        if getattr(args, name) is None:
            setattr(args, name, round(pct * scale, 3))
    if args.trend_windows is None:
        args.trend_windows = max(24, 480 // duration)
    if args.against_min_streak < 1 or args.against_min_move < 0 or not 1 <= args.against_max <= 99:
        raise SystemExit("--against-min-streak must be >= 1, --against-min-move >= 0, --against-max 1-99")
    if args.continue_max_move < 0:
        raise SystemExit("--continue-max-move must be >= 0")
    if args.move_vs_avg < 0 or args.avg_windows < 10:
        raise SystemExit("--move-vs-avg must be >= 0, --avg-windows >= 10")
    if args.trend_windows < 0 or args.trend_against_min_move < 0 or not 0 <= args.take_profit_at <= 99:
        raise SystemExit("--trend-windows and --trend-against-min-move must be >= 0, --take-profit-at 0-99")
    if args.max_prev_move_pct < 0:
        raise SystemExit("--max-prev-move-pct must be >= 0")
    move_scale = MOVE_SCALE.get(duration, (duration / 5) ** 0.5)
    if args.enter_after is None:
        args.enter_after = 0 if strategy == "dual" else int(duration * 60 * DUAL_ENTER_AFTER_PCT)
    if args.quiet_max_pct is None:
        args.quiet_max_pct = 0.0 if strategy == "dual" else round(DUAL_QUIET_MAX_PCT_5M * move_scale, 4)
    if args.enter_after < 0:
        raise SystemExit("--enter-after must be >= 0")
    if args.enter_after >= duration * 60:
        raise SystemExit(f"--enter-after must be below the {duration * 60}s window length")
    if args.quiet_max_pct < 0:
        raise SystemExit("--quiet-max-pct must be >= 0")
    if getattr(args, "dual_structure", "") not in ("", "mixed", "up", "down"):
        raise SystemExit("--dual-structure must be mixed, up, down, or empty")
    if not 0 <= args.cheap_ask < 100:
        raise SystemExit("--cheap-ask must be between 0 and 99")
    if not 2 <= args.pair_cents <= 198:
        raise SystemExit("--pair-cents must be between 2 and 198")
    if args.strand_exit < 0 or args.strand_exit >= duration * 60:
        raise SystemExit(f"--strand-exit must be between 0 and {duration * 60 - 1}")
    if not 1 <= args.scalp_entry <= 99:
        raise SystemExit("--scalp-entry must be between 1 and 99")
    if args.scalp_target != 0 and not args.scalp_entry < args.scalp_target <= 99:
        raise SystemExit("--scalp-target must be 0 (hold) or > --scalp-entry and ≤99")
    if args.scalp_ladder_first != 0 and args.scalp_target != 0 and not (
        args.scalp_entry < args.scalp_ladder_first < args.scalp_target
    ):
        raise SystemExit("--scalp-ladder-first must be 0 (off) or between --scalp-entry and --scalp-target")
    if args.scalp_split_entry != 0 and not 0 < args.scalp_split_entry < args.scalp_entry:
        raise SystemExit("--scalp-split-entry must be 0 (off) or below --scalp-entry")
    if args.scalp_split_entry != 0 and args.scalp_target != 0 and not (
        args.scalp_split_entry < args.scalp_split_target < args.scalp_target
    ):
        raise SystemExit("--scalp-split-target must be between --scalp-split-entry and --scalp-target")
    # Adverse-delta cap: auto per duration unless explicitly set (0 = off).
    if args.scalp_delta_cap is None:
        scalp_delta_cap = {5: 20.0, 15: 50.0, 60: 200.0}.get(duration, 0.0)
    elif args.scalp_delta_cap < 0:
        raise SystemExit("--scalp-delta-cap must be >= 0")
    else:
        scalp_delta_cap = float(args.scalp_delta_cap)
    if strategy != "scalp":
        scalp_delta_cap = 0.0
    rsi_zone = None
    if (args.rsi_zone or "").strip():
        raw = args.rsi_zone.strip().replace(":", "-").replace("–", "-").replace("—", "-")
        parts = [p.strip() for p in raw.split("-") if p.strip()]
        if len(parts) != 2:
            raise SystemExit("--rsi-zone must look like 70-100 (LO-HI)")
        try:
            lo, hi = float(parts[0]), float(parts[1])
        except ValueError:
            raise SystemExit("--rsi-zone must look like 70-100 (LO-HI)")
        if not (0 <= lo <= hi <= 100):
            raise SystemExit("--rsi-zone bounds must satisfy 0 ≤ LO ≤ HI ≤ 100")
        rsi_zone = (lo, hi)
        if strategy != "scalp":
            raise SystemExit("--rsi-zone is scalp-only")
    if args.scalp_exit_left < 0 or args.scalp_exit_left >= duration * 60:
        raise SystemExit(f"--scalp-exit-left must be between 0 and {duration * 60 - 1}")
    if not 0 <= args.scalp_stop < args.scalp_entry:
        raise SystemExit("--scalp-stop must be 0 (off) or below --scalp-entry")
    if args.scalp_stop_confirm < 0:
        raise SystemExit("--scalp-stop-confirm must be >= 0")
    if args.hedge_at and not args.limit < args.hedge_at < 100:
        raise SystemExit("--hedge-at must be above --limit and below 100 (or 0 for off)")
    if not 0 <= args.stop_loss_at < duration * 60:
        raise SystemExit(f"--stop-loss-at must be between 0 and {duration * 60 - 1} seconds")
    if not 0 <= args.cancel_left_pct < 100:
        raise SystemExit("--cancel-left-pct must be between 0 and 100")
    if not 0 <= args.savior_left_pct < 100:
        raise SystemExit("--savior-left-pct must be between 0 and 100")
    if not 0 <= args.savior_force_left_pct < 100:
        raise SystemExit("--savior-force-left-pct must be between 0 and 100")
    if not 0 <= args.savior_stall_pct < 100:
        raise SystemExit("--savior-stall-pct must be between 0 and 100")
    if args.savior_left_pct > 0 and args.savior_force_left_pct >= args.savior_left_pct:
        raise SystemExit("--savior-force-left-pct must be below --savior-left-pct (force is later than arm)")

    updown_file = f"{coin}-{duration}-updown.csv"
    dual_trades_file = f"{coin}-{duration}-predict-dual-trades.csv"
    if not 1 <= args.leader_at <= args.leader_max <= 99:
        raise SystemExit("--leader-at and --leader-max must be between 1 and 99 cents, --leader-max >= --leader-at")
    if args.min_prev_move_pct is None:
        args.min_prev_move_pct = LEADER_MIN_PREV_MOVE_PCT[duration]
    if args.min_prev_move_pct < 0:
        raise SystemExit("--min-prev-move-pct must be >= 0")
    if not 0 <= args.watch_from_pct < args.watch_to_pct < 100:
        raise SystemExit("--watch-from-pct must be below --watch-to-pct, both between 0 and 100")
    if strategy in ("leader", "fav", "against", "combo", "scalp"):
        if strategy == "against" and args.against_filter != "struct_rsi":
            # alt against variants get their own log so they can run beside live struct_rsi
            trades_file = f"{coin}-{duration}-predict-against-{args.against_filter}-trades.csv"
        else:
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
                  savior_left_pct=args.savior_left_pct, savior_force_left_pct=args.savior_force_left_pct,
                  savior_stall_pct=args.savior_stall_pct,
                  hedge_cents=args.hedge_at, stop_loss_seconds=args.stop_loss_at, stop_loss_live=args.stop_loss_live,
                  stop_loss_min_cents=args.stop_loss_min,
                  reload_to=args.capital if args.reload_to is None else args.reload_to,
                  reloaded_total=last_reloaded_total(trades_file))
    if strategy == "leader":
        sim = LeaderSim(*sim_args, **common, leader_at_cents=args.leader_at, leader_max_cents=args.leader_max,
                        min_prev_move_pct=args.min_prev_move_pct, watch_from_pct=args.watch_from_pct,
                        watch_to_pct=args.watch_to_pct)
    elif strategy == "dual":
        # Both@limit; structure gate optional via --dual-structure (default off).
        dual_common = dict(common)
        dual_common["stop_loss_seconds"] = 0
        sim = DualSim(*sim_args, **dual_common, require_structure=args.dual_structure,
                      enter_after_seconds=args.enter_after, quiet_max_pct=args.quiet_max_pct,
                      cheap_cents=args.cheap_ask, pair_cents=args.pair_cents,
                      strand_exit_seconds=args.strand_exit,
                      allow_balanced=True if args.cheap_ask == 0 else args.balanced_dual)
    elif strategy == "scalp":
        # Same optional structure gate as dual; no-fill cancel via --cancel-left-pct.
        scalp_common = dict(common)
        scalp_common["stop_loss_seconds"] = 0
        sim = ScalpSim(*sim_args, **scalp_common, entry_cents=args.scalp_entry,
                       target_cents=args.scalp_target, ladder_first_cents=args.scalp_ladder_first,
                       split_entry_cents=args.scalp_split_entry,
                       split_target_cents=args.scalp_split_target,
                       delta_cap_usd=scalp_delta_cap,
                       exit_left_seconds=args.scalp_exit_left,
                       stop_cents=args.scalp_stop, stop_confirm_seconds=args.scalp_stop_confirm,
                       require_structure=args.dual_structure, rsi_zone=rsi_zone)
    elif strategy == "against":
        sim = AgainstSim(*sim_args, **common, against_min_streak=args.against_min_streak,
                         against_min_move_pct=args.against_min_move, against_max_cents=args.against_max,
                         take_profit_cents=args.take_profit_at, take_profit_all=args.take_profit_all,
                         min_move_vs_avg=args.move_vs_avg, avg_windows=args.avg_windows,
                         against_filter=args.against_filter)
    else:
        sim = ComboSim(*sim_args, **common, strategy=strategy, fav_max_cents=args.fav_max,
                       dual_lookback=args.dual_lookback, against_min_streak=args.against_min_streak,
                       against_min_move_pct=args.against_min_move, against_max_cents=args.against_max,
                       fav_max_move_pct=args.fav_max_move, continue_max_move_pct=args.continue_max_move,
                       trend_windows=args.trend_windows, trend_against_min_move_pct=args.trend_against_min_move,
                       against_stop_after_loss=not args.against_keep_betting)
    sim.load_updown(updown_file)
    if strategy in STRATEGIES:
        if trades_file != dual_trades_file:
            sim.load_history(dual_trades_file)
        sim.load_history(trades_file)
    print(f"[cfg] Predict.fun {strategy} | coin={coin} | duration={duration}m | Chainlink feed {feed_id}")
    print(sim.summarize())
    print(f"[files] updown={updown_file} | trades={trades_file}")

    print("[ws] Initializing Predict.fun socket...")
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
            print("[err] Invalid number format. Please enter a valid decimal price (e.g. 82741.02).")

    ui = PlainUI(sim, coin, duration) if args.plain else Dashboard(sim, coin, duration)
    ui.start()
    # WS reconnect threads must not write the TTY — Rich Live redirects stdout/stderr and freezes if they do.
    ws.set_status_handler(ui.event)
    try:
        while True:
            try:
                current_ptb = run_window(ui, ws, sim, coin, duration, feed_id, updown_file, current_ptb)
            except Exception as e:
                ui.event(f"Error in loop execution: {e}. Restarting cycle...")
                time.sleep(2)
    finally:
        ws.set_status_handler(None)
        ui.stop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
