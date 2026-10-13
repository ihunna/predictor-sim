#!/usr/bin/env python3
"""Fake-tick checks for dual.py and the log formats. Run: python test_dual.py"""
import csv
import os
import tempfile

from dual import TRADES_HEADER, DualSim, last_capital_after
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


def new_sim(tmp, place_at=None, capital=100.0, hedge_cents=0, stop_loss_seconds=0, stop_loss_live=True):
    return DualSim("btc", 5, 30, 8, place_at, os.path.join(tmp, "trades.csv"), capital, "default",
                   hedge_cents=hedge_cents, stop_loss_seconds=stop_loss_seconds, stop_loss_live=stop_loss_live)


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
    # logged for skipped windows too
    sim.start_window(START, END, "s", "1", now=START, skip_reason="no_ptb")
    for off, u, d in ticks:
        sim.on_tick(START + off, u, d)
    assert sim.finish_window().cheap_up[5].bounces == 2


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
            test_cheap_side_bounces,
            test_book_averages,
            test_tie,
            test_log_formats_and_capital_restore,
            test_old_log_upgraded_in_place,
        ):
            test(tempfile.mkdtemp(dir=tmp))
            print(f"✅ {test.__name__}")
        test_book_and_slugs()
        print("✅ test_book_and_slugs")
    print("All tests passed")


if __name__ == "__main__":
    main()
