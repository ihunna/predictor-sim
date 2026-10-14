#!/usr/bin/env python3
"""Fake-tick checks for dual.py and the log formats. Run: python test_dual.py"""
import csv
import os
import tempfile

from dual import TRADES_HEADER, DualSim, last_capital_after, last_reloaded_total
from logs import MARKET_CSV_HEADER, format_lowest_cents, log_to_csv
from predict_feed import best_asks, book_depth, get_current_active_slug, window_bounds

START = 1791813600  # 14:00 UTC = 10am ET
END = START + 300


def run_window(sim, ticks, start=START, end=END):
    """ticks: list of (seconds_into_window, up_ask, down_ask)."""
    sim.start_window(start, end, f"btc-updown-5m-{start}", "1", now=start)
    for offset, up, down in ticks:
        sim.on_tick(start + offset, up, down)
    return sim.finish_window()


def new_sim(tmp, place_at=None, capital=100.0, hedge_cents=0, stop_loss_seconds=0, stop_loss_live=True,
            stop_loss_min_cents=0):
    return DualSim("btc", 5, 30, 8, place_at, os.path.join(tmp, "trades.csv"), capital, "default",
                   hedge_cents=hedge_cents, stop_loss_seconds=stop_loss_seconds, stop_loss_live=stop_loss_live,
                   stop_loss_min_cents=stop_loss_min_cents)


def test_both_fill_and_lowest_times(tmp):
    sim = new_sim(tmp)
    state = run_window(sim, [(0, 0.50, 0.51), (40, 0.29, 0.72), (100, 0.35, 0.66), (200, 0.80, 0.25), (250, 0.85, 0.22)])
    assert state.placed_time_left == 300
    assert state.up_ask_at_place == 0.50 and state.down_ask_at_place == 0.51
    assert state.lowest_up == 0.29 and state.lowest_up_time_left == 260
    assert state.lowest_down == 0.22 and state.lowest_down_time_left == 50
    assert state.up_fill_time_left == 260 and state.down_fill_time_left == 100
    assert state.fill_type == "both"
    row = sim.settle(state, "Down", 82900.0, 82850.0, -12.5)
    assert row["pnl"] == "3.2000", row["pnl"]
    assert row["capital_after"] == "103.2000"


def test_one_side_win_and_loss(tmp):
    sim = new_sim(tmp)
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.28, 0.73), (290, 0.68, 0.33)])
    assert state.fill_type == "only_up"
    assert sim.settle(state, "Up", 1, 2, None)["pnl"] == "5.6000"
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.28, 0.73), (290, 0.68, 0.33)])
    assert sim.settle(state, "Down", 1, 2, None)["pnl"] == "-2.4000"
    assert sim.capital == 103.2


def test_no_fill_and_unknown(tmp):
    sim = new_sim(tmp)
    state = run_window(sim, [(0, 0.50, 0.51), (150, 0.45, 0.56)])
    assert state.fill_type == "none"
    assert sim.settle(state, "Up", 1, 2, None)["pnl"] == "0.0000"
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.20, 0.81)])
    row = sim.settle(state, "UNKNOWN", 0, 0, None)
    assert row["pnl"] == "" and "unsettled" in row["notes"] and sim.capital == 100.0


def test_place_at_and_taker_fill(tmp):
    sim = new_sim(tmp, place_at=120)
    # dips before placement count toward lowest, not toward fills
    state = run_window(sim, [(0, 0.50, 0.51), (30, 0.20, 0.81), (180, 0.25, 0.76), (250, 0.40, 0.61)])
    assert state.lowest_up == 0.20 and state.lowest_up_time_left == 270
    assert state.placed_time_left == 120
    assert state.up_taker and state.up_fill_price == 0.25 and state.up_fill_time_left == 120
    assert state.down_fill_time_left is None
    fee, pnl = sim.compute_result(state, "Up", maker_fee_bps=0, taker_fee_bps=200)
    assert fee == 0.04 and pnl == round(8 - 2.0 - 0.04, 4), (fee, pnl)
    row = sim.settle(state, "Up", 1, 2, None, taker_fee_bps=200)
    assert "up_taker" in row["notes"]


def test_joined_late_not_traded(tmp):
    sim = new_sim(tmp)
    sim.start_window(START, END, f"btc-updown-5m-{START}", "1", now=START + 285)
    sim.on_tick(START + 285, 0.0, 0.01)
    sim.on_tick(START + 290, 0.99, 0.01)
    state = sim.finish_window()
    assert not state.trading and state.placed_time_left is None
    assert state.fill_type == "none" and state.lowest_down == 0.01
    row = sim.settle(state, "Up", 1, 2, None)
    assert row["pnl"] == "0.0000" and row["notes"] == "joined_late_285s;not_traded", row["notes"]
    assert sim.capital == 100.0
    # an on-time start still trades
    assert run_window(sim, [(0, 0.29, 0.72)]).fill_type == "only_up"


def test_no_ptb_not_traded_even_on_time(tmp):
    sim = new_sim(tmp)
    sim.start_window(START, END, f"btc-updown-5m-{START}", "1", now=START, skip_reason="no_ptb")
    sim.on_tick(START + 1, 0.29, 0.72)
    state = sim.finish_window()
    assert not state.trading and state.fill_type == "none" and state.lowest_up == 0.29
    row = sim.settle(state, "Up", 1, 2, None)
    assert row["pnl"] == "0.0000" and row["notes"] == "no_ptb;not_traded", row["notes"]


def test_prev_move_filter(tmp):
    sim = new_sim(tmp)
    assert sim.max_prev_move_pct == 0.10
    assert sim.prev_move_skip_reason(0.05) is None
    assert sim.prev_move_skip_reason(None) == "prev_move_unknown"
    assert sim.prev_move_skip_reason(0.10) == "prev_move_+0.100pct"
    assert sim.prev_move_skip_reason(-0.25) == "prev_move_-0.250pct"
    sim.max_prev_move_pct = 0
    assert sim.prev_move_skip_reason(5.0) is None


def test_decision_pending_holds_orders(tmp):
    sim = new_sim(tmp)
    state = sim.start_window(START, END, f"btc-updown-5m-{START}", "1", now=START, decision_pending=True)
    sim.on_tick(START + 1, 0.25, 0.76)
    assert state.placed_time_left is None and state.lowest_up == 0.25
    sim.resolve_decision(state, None)
    sim.on_tick(START + 4, 0.45, 0.56)
    sim.on_tick(START + 60, 0.29, 0.72)
    assert state.placed_time_left == 296 and state.up_fill_time_left == 240 and not state.up_taker
    sim.finish_window()

    state = sim.start_window(START, END, f"btc-updown-5m-{START}", "1", now=START, decision_pending=True)
    sim.resolve_decision(state, "prev_move_+0.150pct")
    sim.on_tick(START + 60, 0.20, 0.81)
    assert not state.trading and state.fill_type == "none"
    assert state.notes == ["prev_move_+0.150pct", "not_traded"]


def test_cancel_when_nothing_fills(tmp):
    sim = new_sim(tmp)
    assert sim.cancel_left_seconds() == 120
    state = sim.start_window(START, END, f"btc-updown-5m-{START}", "1", now=START)
    sim.on_tick(START + 1, 0.50, 0.51)
    sim.on_tick(START + 179, 0.45, 0.56)
    assert state.cancelled_time_left is None
    sim.on_tick(START + 180, 0.45, 0.56)
    sim.on_tick(START + 250, 0.20, 0.81)
    assert state.cancelled_time_left == 120 and state.fill_type == "none" and state.lowest_up == 0.20
    assert "cancelled_no_fill_at_02:00_left" in state.notes
    sim.finish_window()

    state = sim.start_window(START, END, f"btc-updown-5m-{START}", "1", now=START)
    sim.on_tick(START + 1, 0.50, 0.51)
    sim.on_tick(START + 60, 0.29, 0.72)
    sim.on_tick(START + 250, 0.80, 0.21)
    assert state.cancelled_time_left is None and state.fill_type == "both"
    sim.finish_window()

    sim15 = DualSim("btc", 15, 30, 8, None, os.path.join(tmp, "t15.csv"), 100, "test")
    assert sim15.cancel_left_seconds() == 360


def test_hedge_split(tmp):
    sim = new_sim(tmp, hedge_cents=50)
    assert sim.hedge_contracts(2.40) == 5 and sim.hedge_contracts(8.0) == 8

    # Up fills at 30¢; Down then only reaches 40¢: 5 @ 50¢ lock-in fills, 3 @ 30¢ stay open
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.30, 0.71), (120, 0.62, 0.40), (290, 0.62, 0.39)])
    hedge = state.hedge_order
    assert hedge.side == "Down" and hedge.contracts == 5 and hedge.fill_price == 0.50 and not hedge.taker
    assert hedge.fill_time_left == 180 and state.hedge_time_left == 240
    profit = next(o for o in state.orders if o.kind == "profit")
    assert profit.side == "Down" and profit.contracts == 3 and not profit.filled
    assert state.contracts("Up") == 8 and state.contracts("Down") == 5
    assert sim.compute_result(state, "Up")[1] == 3.1
    assert sim.compute_result(state, "Down")[1] == 0.1
    row = sim.settle(state, "Down", 1, 2, None)
    assert row["down_contracts"] == 5 and row["down_avg_cents"] == "50.0" and row["hedge_contracts"] == 5
    assert row["other_low_after_first_fill"] == 39 and row["other_low_after_first_fill_time_left"] == 10
    assert "hedge_down_5@50" in row["notes"]

    # Down keeps falling to 28¢: both second-side orders fill, +2.20 locked
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.30, 0.71), (120, 0.62, 0.40), (200, 0.73, 0.28)])
    assert state.contracts("Down") == 8 and abs(state.avg_price("Down") - (5 * 0.5 + 3 * 0.3) / 8) < 1e-9
    assert sim.compute_result(state, "Up")[1] == 2.2 and sim.compute_result(state, "Down")[1] == 2.2

    # Down never reaches 50¢: same as no hedge
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.30, 0.71), (200, 0.40, 0.61)])
    assert state.contracts("Down") == 0
    assert sim.compute_result(state, "Down")[1] == -2.4 and sim.compute_result(state, "Up")[1] == 5.6

    # both sides fill on the same tick: no hedge
    state = run_window(sim, [(0, 0.29, 0.29)])
    assert state.hedge_order is None and state.contracts("Up") == state.contracts("Down") == 8


def test_stop_loss(tmp):
    sim = new_sim(tmp, stop_loss_seconds=30)
    # only Up filled, Down leads at 30s left: sell 8 Up at its bid (1 - Down ask 0.85 = 15¢)
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.30, 0.71), (269, 0.20, 0.81), (270, 0.16, 0.85),
                             (290, 0.05, 0.96)])
    assert state.stop_side == "Up" and state.stop_contracts == 8 and state.stop_price == 0.15
    assert state.stop_time_left == 30 and state.held("Up") == 0
    assert sim.compute_result(state, "Down")[1] == round(1.20 - 2.40, 4)
    fee, pnl = sim.compute_result(state, "Down", taker_fee_bps=200)
    assert fee == 0.024 and pnl == round(1.20 - 2.40 - 0.024, 4)
    row = sim.settle(state, "Down", 1, 2, None)
    assert row["stop_side"] == "Up" and row["stop_contracts"] == 8 and row["stop_price_cents"] == 15

    # Up filled and Up leads at 30s left: not losing, nothing sold
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.30, 0.71), (270, 0.85, 0.16)])
    assert state.stop_side is None and state.stop_checked

    # both filled: locked profit, nothing sold
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.30, 0.71), (100, 0.75, 0.28), (270, 0.10, 0.91)])
    assert state.stop_side is None

    # hedged 8 Up / 5 Down, Up leading at 30s left: if Up wins +3.10, not losing
    sim.hedge_cents, sim.hedge = 50, 0.50
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.30, 0.71), (120, 0.62, 0.40), (270, 0.80, 0.35)])
    assert state.held("Down") == 5 and state.stop_side is None

    # only checked once: losing only after 30s left is not sold
    sim.hedge_cents, sim.hedge = 0, 0.0
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.30, 0.71), (270, 0.80, 0.21), (290, 0.05, 0.96)])
    assert state.stop_side is None


def test_stop_loss_logged_only(tmp):
    sim = new_sim(tmp, stop_loss_seconds=20, stop_loss_live=False)
    ticks = [(0, 0.50, 0.51), (60, 0.30, 0.71), (280, 0.16, 0.85), (290, 0.05, 0.96)]
    state = run_window(sim, ticks)
    assert state.stop_side is None and state.held("Up") == 8
    assert state.would_stop == ("Up", 8, 0.15, 20)
    row = sim.settle(state, "Down", 1, 2, None, taker_fee_bps=200)
    assert row["pnl"] == "-2.4000" and row["would_stop_side"] == "Up" and row["would_stop_price_cents"] == 15
    assert row["would_stop_saved"] == "+1.1760", row["would_stop_saved"]
    assert "would_stop_sell_up_8@15_at_00:20_left" in row["notes"]
    state = run_window(sim, ticks)
    assert sim.would_stop_saved(state, "Up") == round(1.20 - 8, 4)


def test_stop_loss_minimum_price(tmp):
    # live stop with a 3¢ minimum: Up's bid is 2¢ with 20s left, so the contracts are kept and only logged
    sim = new_sim(tmp, stop_loss_seconds=20, stop_loss_min_cents=3)
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.30, 0.71), (280, 0.02, 0.98)])
    assert state.stop_side is None and state.held("Up") == 8
    assert state.would_stop == ("Up", 8, 0.02, 20)
    row = sim.settle(state, "Down", 1, 2, None)
    assert row["stop_side"] == "" and row["would_stop_side"] == "Up"
    assert "stop_skipped_below_3c_up_8@2_at_00:20_left" in row["notes"]
    # at 15¢ it sells as before
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.30, 0.71), (280, 0.16, 0.85)])
    assert state.stop_side == "Up" and state.stop_contracts == 8 and state.would_stop is None


def test_stop_checkpoints(tmp):
    # Up fills at 30¢; losing at 60s and 50s left; Down then fills at 30¢ (dual) so later checks don't fire
    sim = new_sim(tmp)
    ticks = [(0, 0.50, 0.51), (60, 0.30, 0.71), (240, 0.20, 0.81), (250, 0.25, 0.76), (260, 0.72, 0.29),
             (270, 0.80, 0.21), (280, 0.85, 0.16)]
    state = run_window(sim, ticks)
    assert state.stop_checks[60][:3] == ("Up", 8, 0.19) and state.stop_checks[50][:3] == ("Up", 8, 0.24)
    assert 40 not in state.stop_checks and 30 not in state.stop_checks and 20 not in state.stop_checks
    row = sim.settle(state, "Down", 1, 2, None)
    # stopping at 60s: sell 8 Up @19¢ and cancel Down → 1.52 - 2.40 = -0.88; holding the dual: +3.20
    assert row["stop_60s_side"] == "Up" and row["stop_60s_price_cents"] == 19
    assert row["stop_60s_saved"] == "-4.0800", row["stop_60s_saved"]
    assert row["stop_50s_saved"] == "-3.6800" and row["stop_40s_side"] == ""
    # one-sided loser with the live 20s stop: checkpoints are measured against holding, not the live sale
    sim = new_sim(tmp, stop_loss_seconds=20)
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.30, 0.71), (270, 0.10, 0.91), (280, 0.06, 0.95)])
    row = sim.settle(state, "Down", 1, 2, None)
    assert row["stop_30s_price_cents"] == 9 and row["stop_30s_saved"] == "+0.7200"
    assert row["stop_20s_price_cents"] == 5 and row["stop_20s_saved"] == "+0.4000"
    assert row["stop_side"] == "Up" and row["pnl"] == "-2.0000"


def test_reload_when_balance_too_low(tmp):
    sim = DualSim("btc", 5, 30, 8, None, os.path.join(tmp, "trades.csv"), 3.0, "log", reload_to=100.0)
    state = sim.start_window(START, END, "s", "1", now=START)
    assert state.reload == 97.0 and sim.capital == 100.0 and sim.reloaded_total == 97.0
    row = sim.settle(sim.finish_window(), "Up", 1, 2, None)
    assert row["reload"] == "97.0000" and row["reloaded_total"] == "97.0000" and row["capital_before"] == "100.0000"
    assert "reloaded_+97.00" in row["notes"]
    # enough for both orders ($4.80): no reload; restart picks up the running total from the log
    sim.capital = 4.80
    state = sim.start_window(START + 300, END + 300, "s", "2", now=START + 300)
    assert state.reload == 0.0 and sim.capital == 4.80
    sim.settle(sim.finish_window(), "Up", 1, 2, None)
    assert last_reloaded_total(os.path.join(tmp, "trades.csv")) == 97.0
    # off by default
    sim = DualSim("btc", 5, 30, 8, None, os.path.join(tmp, "t2.csv"), -1.0, "log")
    assert sim.start_window(START, END, "s", "1", now=START).reload == 0.0 and sim.capital == -1.0


def test_cheap_side_bounces(tmp):
    sim = new_sim(tmp)
    # Up drops to 5¢ at 200s left, its bid (1 - Down ask) bounces to 11¢, 7¢, 12¢, 13¢, then dies
    ticks = [(0, 0.50, 0.51), (50, 0.07, 0.94), (100, 0.05, 0.96), (120, 0.12, 0.89), (130, 0.08, 0.93),
             (140, 0.13, 0.88), (150, 0.14, 0.87), (200, 0.02, 0.99)]
    state = run_window(sim, ticks)
    up = state.cheap_up[5]
    assert up.touch_time_left == 200 and up.max_bid == 0.13 and up.max_bid_time_left == 150 and up.bounces == 2
    # 20¢ level: touched at 250s left (7¢), best bid 13¢, never back to 41¢
    up20 = state.cheap_up[20]
    assert up20.touch_time_left == 250 and up20.max_bid == 0.13 and up20.bounces == 0
    assert state.cheap_down[20].touch_time_left is None
    row = sim.settle(state, "Down", 1, 2, None)
    assert row["up_touch_5c_time_left"] == 200 and row["up_max_bid_after_5c"] == 13
    assert row["up_bounces_11c_after_5c"] == 2 and row["down_touch_5c_time_left"] == ""
    assert row["up_touch_10c_time_left"] == 250 and row["up_bounces_21c_after_10c"] == 0
    assert row["up_max_bid_after_20c"] == 13 and row["down_bounces_41c_after_20c"] == ""
    # 40¢ level: Up first at 7¢ (250s left) too, since the 50¢ open never reached 40¢
    assert row["up_touch_40c_time_left"] == 250 and row["up_max_bid_after_30c"] == 13
    assert row["up_bounces_81c_after_40c"] == 0 and row["down_touch_30c_time_left"] == ""
    # logged for skipped windows too
    sim.start_window(START, END, "s", "1", now=START, skip_reason="no_ptb")
    for off, u, d in ticks:
        sim.on_tick(START + off, u, d)
    assert sim.finish_window().cheap_up[5].bounces == 2


def test_rise_then_fall(tmp):
    sim = new_sim(tmp)
    # Up's bid (1 - Down ask) rises to 66¢ at 200s left, dips to 52¢ at 150s, recovers to 80¢, ends 70¢
    ticks = [(0, 0.52, 0.50), (50, 0.60, 0.42), (100, 0.67, 0.34), (150, 0.50, 0.48), (200, 0.82, 0.20), (290, 0.72, 0.30)]
    state = run_window(sim, ticks)
    r60, r65, r75 = state.rise_up[60], state.rise_up[65], state.rise_up[75]
    assert r60.touch_time_left == 200 and r60.min_bid == 0.52 and r60.min_bid_time_left == 150
    assert r65.touch_time_left == 200 and r65.min_bid == 0.52
    assert r75.touch_time_left == 100 and r75.min_bid == 0.70 and r75.min_bid_time_left == 10
    assert state.rise_down[60].touch_time_left is None
    row = sim.settle(state, "Up", 1, 2, None)
    assert row["up_rise_60c_time_left"] == 200 and row["up_min_bid_after_60c"] == 52
    assert row["up_min_bid_after_75c"] == 70 and row["down_rise_60c_time_left"] == ""


def test_book_averages(tmp):
    sim = new_sim(tmp)
    state = sim.start_window(START, END, "s", "1", now=START)
    state.record_book({"up_ask_size": 100, "up_ask_depth": 300, "down_ask_size": 50, "down_ask_depth": 200}, 0.52, 0.50)
    state.record_book({"up_ask_size": 200, "up_ask_depth": 500, "down_ask_size": 150, "down_ask_depth": 400}, 0.51, 0.51)
    state.record_book(None, 0.0, 0.5)
    state.volume_early_usd, state.volume_early_time_left = 1234.5, 240
    row = sim.settle(sim.finish_window(), "Up", 1, 2, None)
    assert row["up_ask_size_avg"] == "150.00" and row["up_ask_depth_5c_avg"] == "400.00"
    assert row["down_ask_depth_5c_avg"] == "300.00" and row["book_samples"] == 2
    assert row["spread_cents_avg"] == "2.00", row["spread_cents_avg"]
    assert row["volume_usd_early"] == "1234.50" and row["volume_early_time_left"] == 240


def test_open_and_end_asks(tmp):
    sim = new_sim(tmp)
    state = sim.start_window(START, END, "s", "1", now=START)
    state.preopen_asks = (0.56, 0.45)
    for off, u, d in [(0, 0.0, 0.5), (1, 0.55, 0.47), (100, 0.40, 0.61), (291, 0.80, 0.21), (295, 0.90, 0.11)]:
        sim.on_tick(START + off, u, d)
    row = sim.settle(sim.finish_window(), "Up", 1, 2, None)
    assert row["preopen_up_ask"] == 56 and row["preopen_down_ask"] == 45
    assert row["open_up_ask"] == 55 and row["open_down_ask"] == 47 and row["open_time_left"] == 299
    assert row["up_ask_10s_left"] == 80 and row["down_ask_10s_left"] == 21
    # decided window: Down's book is empty with 10s left, Up's price is still logged
    sim.start_window(START + 300, END + 300, "s", "2", now=START + 300)
    for off, u, d in [(0, 0.5, 0.51), (292, 0.97, 0.0)]:
        sim.on_tick(START + 300 + off, u, d)
    row = sim.settle(sim.finish_window(), "Up", 1, 2, None)
    assert row["up_ask_10s_left"] == 97 and row["down_ask_10s_left"] == ""


def test_snapshots_into_window(tmp):
    sim = new_sim(tmp)
    sim.start_window(START, END, "s", "1", now=START)
    ptb = 100000.0
    for off, u, d, btc in [(0, 0.50, 0.51, 100000.0), (11, 0.51, 0.50, 100005.0), (29, 0.52, 0.49, 100010.0),
                           (31, 0.58, 0.43, 100020.0), (35, 0.10, 0.91, 99000.0), (62, 0.66, 0.35, 100050.0),
                           (200, 0.70, 0.31, 0.0)]:
        sim.on_tick(START + off, u, d, btc, ptb)
    row = sim.settle(sim.finish_window(), "Up", 1, 2, None)
    assert row["up_ask_10s_in"] == 51 and row["btc_move_pct_10s_in"] == "+0.0050"
    assert row["up_ask_30s_in"] == 58 and row["down_ask_30s_in"] == 43 and row["btc_move_pct_30s_in"] == "+0.0200"
    assert row["up_ask_60s_in"] == 66 and row["btc_move_pct_60s_in"] == "+0.0500"
    # no tick within 5s after 20s, 40s, 50s, 90s...: left blank rather than filled with a late price
    assert row["up_ask_20s_in"] == "" and row["up_ask_40s_in"] == "" and row["up_ask_50s_in"] == ""
    assert row["up_ask_90s_in"] == "" and row["btc_move_pct_120s_in"] == ""
    # BTC went 1% below the price to beat at 35s in (265s left), then 0.05% above at 62s in (238s left)
    assert row["btc_down_0.05pct_time_left"] == 265 and row["btc_up_0.05pct_time_left"] == 238
    sim.start_window(START + 300, END + 300, "s", "2", now=START + 300)
    sim.on_tick(START + 310, 0.5, 0.51, 100040.0, ptb)
    row = sim.settle(sim.finish_window(), "Up", 1, 2, None)
    assert row["btc_up_0.05pct_time_left"] == "" and row["btc_down_0.05pct_time_left"] == ""


def test_tie(tmp):
    sim = new_sim(tmp)
    state = run_window(sim, [(0, 0.29, 0.29)])
    assert state.fill_type == "both"
    assert sim.compute_result(state, "Tie")[1] == round(8 - 0.58 * 8, 4)


def test_log_formats_and_capital_restore(tmp):
    sim = new_sim(tmp)
    sim.settle(run_window(sim, [(0, 0.50, 0.51), (60, 0.28, 0.73)]), "Up", 1, 2, 0.5)
    sim.settle(run_window(sim, [(0, 0.50, 0.51)]), "Down", 2, 1, None)
    path = os.path.join(tmp, "trades.csv")
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == TRADES_HEADER
    assert all(len(r) == len(TRADES_HEADER) for r in rows)
    assert len(rows) == 3
    assert last_capital_after(path, 50.0) == (105.6, "log")

    updown = os.path.join(tmp, "btc-5-updown.csv")
    log_to_csv(updown, START, 82900.123, 82850.0, format_lowest_cents(0.29), format_lowest_cents(float("inf")), "Down")
    with open(updown, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == MARKET_CSV_HEADER
    assert rows[1] == [str(START), "$82900.12", "$82850.00", "29¢", "N/A", "Down"], rows[1]


def test_old_log_upgraded_in_place(tmp):
    path = os.path.join(tmp, "trades.csv")
    old_header = [c for c in TRADES_HEADER if c not in ("up_contracts", "hedge_cents")] + ["retired"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=old_header)
        writer.writeheader()
        writer.writerow({c: "" for c in old_header} | {"window_start": "1", "capital_after": "111.0", "retired": "x"})
    sim = new_sim(tmp)
    sim.settle(run_window(sim, [(0, 0.50, 0.51)]), "Up", 1, 2, None)
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert not [p for p in os.listdir(tmp) if p.endswith(".bak")]
    assert len(rows) == 2 and list(rows[0])[:len(TRADES_HEADER)] == TRADES_HEADER
    assert rows[0]["window_start"] == "1" and rows[0]["up_contracts"] == "none" and rows[0]["retired"] == "x"
    assert rows[1]["window_start"] == str(START) and rows[1]["up_contracts"] == "0" and rows[1]["retired"] == "none"
    assert last_capital_after(path, 0) == (100.0, "log")


def test_book_and_slugs():
    assert best_asks({"asks": [[0.31, 10]], "bids": [[0.27, 5]]}) == (0.31, 0.73)
    assert best_asks({"asks": [], "bids": []}) == (0.0, 0.0)
    depth = book_depth({"asks": [[0.30, 340], [0.31, 30], [0.36, 50]], "bids": [[0.29, 95], [0.24, 85], [0.20, 9]]})
    assert depth == {"up_ask_size": 340, "up_ask_depth": 370, "down_ask_size": 95, "down_ask_depth": 180}, depth
    assert book_depth({"asks": [], "bids": []})["up_ask_depth"] == 0.0
    assert get_current_active_slug("btc", 5, at=START + 10) == "btc-updown-5m-1791813600"
    assert get_current_active_slug("eth", 15, at=START + 899) == "eth-updown-15m-1791813600"
    assert get_current_active_slug("btc", 60, at=START + 5) == "bitcoin-up-or-down-october-12-2026-10am-et"
    assert window_bounds(60, at=START + 1800) == (START, START + 3600)


def regime_sim(tmp, **kwargs):
    sim = new_sim(tmp, **kwargs)
    sim.strategy = "regime"
    return sim


def test_regime_decision(tmp):
    sim = regime_sim(tmp)
    assert sim.regime_decision(START) == ("skip", "streak_unknown")
    sim.history = {START - 300: ["Up", False], START - 600: ["Up", False], START - 900: ["Down", True]}
    assert sim.regime_decision(START) == ("fav", "streak_2_up")
    sim.history = {START - 300: ["Up", False], START - 600: ["Up", False], START - 900: ["Up", False],
                   START - 1200: ["Down", True]}
    assert sim.regime_decision(START) == ("skip", "no_dual_in_last_3")
    sim.history = {START - 300: ["Down", False], START - 600: ["Up", True]}
    assert sim.regime_decision(START) == ("dual", "dual_in_last_3")
    sim.history = {START - 300: ["Down", False], START - 600: ["Up", False]}
    assert sim.regime_decision(START) == ("skip", "recent_duals_unknown")
    # the last window's winner comes from its official prices before it settles
    sim.history = {START - 300: [None, False], START - 600: ["Up", True], START - 900: ["Down", False]}
    sim.start_window(START, END, "s", "1", now=START, decision_pending=True)
    assert sim.regime_decision(START, "Up") == ("fav", "streak_2_up")
    assert sim.current.streak_before == "2 Up" and sim.current.recent_duals == "1/3"


def test_regime_favourite_window(tmp):
    sim = regime_sim(tmp, stop_loss_seconds=20)
    state = sim.start_window(START, END, "s", "1", now=START, decision_pending=True)
    state.action, state.action_reason = "fav", "streak_2_up"
    sim.resolve_decision(state, None, mode="fav")
    for offset, up, down in ((1, 0.47, 0.54), (60, 0.30, 0.71), (290, 0.96, 0.05)):
        sim.on_tick(START + offset, up, down)
    state = sim.finish_window()
    assert state.fill_type == "only_down" and state.contracts("Down") == 8 and state.down_taker
    assert state.stop_side is None and not state.would_stop
    row = sim.settle(state, "Down", 1, 2, None, taker_fee_bps=200)
    assert row["pnl"] == "3.5936" and row["action"] == "fav" and row["down_avg_cents"] == "54.0"
    assert sim.history[START] == ["Down", True]

    state = sim.start_window(START + 300, END + 300, "s", "1", now=START + 300, decision_pending=True)
    sim.resolve_decision(state, None, mode="fav")
    sim.on_tick(START + 301, 0.40, 0.58)
    assert not state.trading and "fav_down_58c_over_55c" in state.notes and not state.orders


def test_regime_history_from_log(tmp):
    sim = regime_sim(tmp)
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.28, 0.73), (200, 0.73, 0.28)])
    assert sim.history[START] == [None, True]
    sim.settle(state, "Up", 1, 2, None)
    state = run_window(sim, [(0, 0.50, 0.51), (60, 0.45, 0.56)], start=START + 300, end=END + 300)
    sim.settle(state, "Down", 1, 2, None)
    loaded = regime_sim(tmp)
    loaded.load_history(sim.trades_file)
    assert loaded.history == {START: ["Up", True], START + 300: ["Down", False]}
    assert loaded.regime_decision(START + 600) == ("dual", "dual_in_last_3")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        for test in (
            test_both_fill_and_lowest_times,
            test_one_side_win_and_loss,
            test_no_fill_and_unknown,
            test_place_at_and_taker_fill,
            test_joined_late_not_traded,
            test_no_ptb_not_traded_even_on_time,
            test_prev_move_filter,
            test_decision_pending_holds_orders,
            test_cancel_when_nothing_fills,
            test_hedge_split,
            test_stop_loss,
            test_stop_loss_logged_only,
            test_stop_loss_minimum_price,
            test_stop_checkpoints,
            test_reload_when_balance_too_low,
            test_cheap_side_bounces,
            test_rise_then_fall,
            test_book_averages,
            test_open_and_end_asks,
            test_snapshots_into_window,
            test_tie,
            test_log_formats_and_capital_restore,
            test_old_log_upgraded_in_place,
            test_regime_decision,
            test_regime_favourite_window,
            test_regime_history_from_log,
        ):
            test(tempfile.mkdtemp(dir=tmp))
            print(f"✅ {test.__name__}")
        test_book_and_slugs()
        print("✅ test_book_and_slugs")
    print("All tests passed")


if __name__ == "__main__":
    main()
