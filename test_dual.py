#!/usr/bin/env python3
"""Fake-tick checks for sim/strategies and the log formats. Run: python test_dual.py"""
import csv
import os
import tempfile

from logs import MARKET_CSV_HEADER, format_lowest_cents, log_to_csv
from predict_feed import best_asks, book_depth, get_current_active_slug, window_bounds
from sim import TRADES_HEADER, last_capital_after, last_reloaded_total
from strategies import AgainstSim, ComboSim, DualSim, LeaderSim, ScalpSim

START = 1791813600  # 14:00 UTC = 10am ET
END = START + 300


def run_window(sim, ticks, start=START, end=END, btc=None, ptb=None):
    """ticks: list of (seconds_into_window, up_ask, down_ask). Optional flat btc/ptb for quiet gate."""
    sim.start_window(start, end, f"btc-updown-5m-{start}", "1", now=start)
    for offset, up, down in ticks:
        sim.on_tick(start + offset, up, down,
                    btc if btc is not None else 0.0,
                    ptb if ptb is not None else 0.0)
    return sim.finish_window()


def new_sim(tmp, place_at=None, capital=100.0, hedge_cents=0, stop_loss_seconds=0, stop_loss_live=True,
            stop_loss_min_cents=0, enter_after_seconds=0, quiet_max_pct=0.0, cheap_cents=0):
    """Default gates off so older dual fill tests stay place-at-start / both-limits."""
    return DualSim("btc", 5, 30, 8, place_at, os.path.join(tmp, "trades.csv"), capital, "default",
                   hedge_cents=hedge_cents, stop_loss_seconds=stop_loss_seconds, stop_loss_live=stop_loss_live,
                   stop_loss_min_cents=stop_loss_min_cents, enter_after_seconds=enter_after_seconds,
                   quiet_max_pct=quiet_max_pct, cheap_cents=cheap_cents)


def quiet_sim(tmp, limit=40, place_at=None, enter_after_seconds=0, quiet_max_pct=0.01, cheap_cents=30,
              pair_cents=80, strand_exit_seconds=60, hedge_cents=0, allow_balanced=True):
    """Live dual: |Δ|≤quiet + ask≤cheap → lift cheap + other @ pair−ask; else both @ limit."""
    return DualSim("btc", 5, limit, 8, place_at, os.path.join(tmp, "trades.csv"), 100.0, "default",
                   hedge_cents=hedge_cents, enter_after_seconds=enter_after_seconds,
                   quiet_max_pct=quiet_max_pct, cheap_cents=cheap_cents, pair_cents=pair_cents,
                   strand_exit_seconds=strand_exit_seconds, allow_balanced=allow_balanced,
                   cancel_left_pct=0.0)


PTB = 100_000.0


def test_quiet_dual_rest_both(tmp):
    """Quiet + balanced book → rest both limits."""
    sim = quiet_sim(tmp, limit=40)
    state = run_window(sim, [
        (0, 0.50, 0.51),
        (30, 0.50, 0.51),
        (60, 0.50, 0.51),
        (100, 0.39, 0.62),
        (150, 0.62, 0.38),
    ], btc=PTB, ptb=PTB)
    assert state.placed_time_left == 300
    assert state.action == "dual" and state.action_reason == "rest_both"
    assert "rest_both" in state.notes
    assert state.fill_type == "both"
    assert not state.up_taker and not state.down_taker


def test_dual_skew_lift_and_other(tmp):
    """Quiet + ask≤30¢ → lift cheap at ask and rest other at pair−ask (equal size)."""
    # allow_balanced=False so the pre-skew book does not rest both @ limit first
    sim = quiet_sim(tmp, limit=40, pair_cents=80, allow_balanced=False)
    state = run_window(sim, [
        (0, 0.50, 0.51),
        (10, 0.28, 0.73),   # Up cheap → lift Up @28, rest Down @52
        (80, 0.80, 0.50),   # Down ask ≤52 → maker fill @52
        (100, 0.80, 0.51),
    ], btc=PTB, ptb=PTB)
    assert state.action == "dual_skew"
    assert "lift_up_28c_other_52c" in state.action_reason
    assert state.up_fill_price == 0.28 and state.up_taker
    assert state.down_fill_price == 0.52
    assert state.fill_type == "both"
    row = sim.settle(state, "Down", PTB, PTB, 0.0)
    # cost 0.28*8 + 0.52*8 = 6.40; payout 8 → +1.60
    assert row["pnl"] == "1.6000", row["pnl"]


def test_dual_skew_strand_exit(tmp):
    """Cheap fills, other never does → flatten cheap at strand_exit."""
    sim = quiet_sim(tmp, limit=40, pair_cents=80, strand_exit_seconds=60)
    state = run_window(sim, [
        (0, 0.28, 0.73),    # lift Up @28, rest Down @52
        (100, 0.90, 0.80),  # Down never ≤52
        (250, 0.95, 0.90),  # at 50s left (<60) strand sell Up at bid 1-0.90=0.10
    ], btc=PTB, ptb=PTB)
    assert state.action == "dual_skew"
    assert state.fill_type == "only_up"
    assert state.stop_side == "Up" and state.stop_contracts == 8
    assert any("strand_sold_up" in n for n in state.notes)


def test_dual_waits_when_delta_loud(tmp):
    """|Δ| above quiet max → no place even if ask is cheap."""
    sim = quiet_sim(tmp, quiet_max_pct=0.01)
    state = run_window(sim, [
        (0, 0.28, 0.73),
        (60, 0.28, 0.73),
        (120, 0.25, 0.76),
    ], btc=PTB * 1.0005, ptb=PTB)
    assert state.placed_time_left is None
    assert "never_placed" in sim.settle(state, "Up", PTB, PTB * 1.0005, 0.05)["notes"]


def test_quiet_dual_waits_for_enter_after(tmp):
    """Does not place before enter_after even if quiet and book is fine."""
    sim = quiet_sim(tmp, enter_after_seconds=60)
    state = run_window(sim, [
        (0, 0.28, 0.73),
        (30, 0.28, 0.73),
        (59, 0.28, 0.73),
    ], btc=PTB, ptb=PTB)
    assert state.placed_time_left is None
    assert "never_placed" in sim.settle(state, "Up", PTB, PTB, 0.0)["notes"]


def scalp_sim(tmp, entry=20, target=30, exit_left=30, stop=10, confirm=5):
    return ScalpSim("btc", 5, 40, 8, None, os.path.join(tmp, "trades.csv"), 100.0, "default",
                    entry_cents=entry, target_cents=target, exit_left_seconds=exit_left,
                    stop_cents=stop, stop_confirm_seconds=confirm)


def test_scalp_tp(tmp):
    """Buy ≤20¢ → sell when bid ≥30¢."""
    sim = scalp_sim(tmp)
    # Up ask 18 → buy; then Down ask 68 → Up bid = 32 ≥ 30 → TP
    state = run_window(sim, [
        (0, 0.50, 0.51),
        (20, 0.18, 0.83),
        (60, 0.40, 0.68),
    ])
    assert state.action == "scalp" and state.up_fill_price == 0.18 and state.up_taker
    assert state.tp_side == "Up" and state.tp_contracts == 8
    row = sim.settle(state, "Down", 1, 2, None)
    # cost 1.44, TP sale 0.30*8=2.40 → +0.96
    assert float(row["pnl"]) == 0.96


def test_scalp_ignores_brief_wick(tmp):
    """Bid dips under stop for < confirm seconds → stay in; later TP."""
    sim = scalp_sim(tmp, stop=10, confirm=5)
    state = run_window(sim, [
        (0, 0.18, 0.83),          # buy Up @18
        (10, 0.50, 0.95),         # bid=5 ≤10 — start confirm clock
        (12, 0.50, 0.95),         # still ≤10 but only 2s — do not stop
        (14, 0.45, 0.70),         # bid=30 — recover; TP
    ])
    assert state.stop_time_left is None
    assert state.tp_side == "Up"


def test_scalp_confirmed_stop(tmp):
    """Bid ≤ stop for ≥ confirm seconds → flatten at live bid."""
    sim = scalp_sim(tmp, stop=10, confirm=5)
    state = run_window(sim, [
        (0, 0.18, 0.83),
        (10, 0.50, 0.95),   # bid=5
        (16, 0.50, 0.95),   # 6s later still ≤10 → stop
    ])
    assert state.stop_side == "Up" and state.stop_price == 0.05
    assert any("stop_sold_up" in n for n in state.notes)


def test_scalp_time_exit(tmp):
    """No TP → flatten at exit_left on live bid."""
    sim = scalp_sim(tmp, exit_left=30, stop=0)
    state = run_window(sim, [
        (0, 0.18, 0.83),
        (100, 0.40, 0.80),   # bid=20, no TP
        (280, 0.50, 0.75),   # 20s left < 30 → time exit bid=25
    ])
    assert state.stop_side == "Up" and abs(state.stop_price - 0.25) < 1e-9
    assert any("time_sold_up" in n for n in state.notes)


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
    assert sim.max_prev_move_pct == 0.0
    assert sim.prev_move_skip_reason(5.0) is None
    assert sim.prev_move_skip_reason(None) is None
    sim.max_prev_move_pct = 0.10
    assert sim.prev_move_skip_reason(0.05) is None
    assert sim.prev_move_skip_reason(None) == "prev_move_unknown"
    assert sim.prev_move_skip_reason(0.10) == "prev_move_+0.100pct"
    assert sim.prev_move_skip_reason(-0.25) == "prev_move_-0.250pct"


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
    # signed delta + asks only when the rounded % changes
    assert row["btc_delta_changes"] == (
        "0s:+0.0000@50/51;11s:+0.0050@51/50;29s:+0.0100@52/49;"
        "31s:+0.0200@58/43;35s:-1.0000@10/91;62s:+0.0500@66/35"
    ), row["btc_delta_changes"]
    sim.start_window(START + 300, END + 300, "s", "2", now=START + 300)
    sim.on_tick(START + 310, 0.5, 0.51, 100040.0, ptb)
    row = sim.settle(sim.finish_window(), "Up", 1, 2, None)
    assert row["btc_up_0.05pct_time_left"] == "" and row["btc_down_0.05pct_time_left"] == ""
    assert row["btc_delta_changes"] == "10s:+0.0400@50/51", row["btc_delta_changes"]


def test_btc_delta_changes_on_change_only(tmp):
    """Same rounded delta is not re-logged; sign shows direction; asks snapshotted on change."""
    sim = new_sim(tmp)
    sim.start_window(START, END, "s", "1", now=START)
    ptb = 100000.0
    ticks = [
        (0, 0.50, 0.51, 100000.0),
        (5, 0.50, 0.51, 100000.0),
        (10, 0.48, 0.53, 100010.0),
        (15, 0.47, 0.54, 100010.0),  # asks move but delta same → no new event
        (40, 0.55, 0.46, 99990.0),
    ]
    for off, u, d, btc in ticks:
        sim.on_tick(START + off, u, d, btc, ptb)
    row = sim.settle(sim.finish_window(), "Up", 1, 2, None)
    assert row["btc_delta_changes"] == "0s:+0.0000@50/51;10s:+0.0100@48/53;40s:-0.0100@55/46", row[
        "btc_delta_changes"
    ]


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
    log_to_csv(updown, START, 82900.123, 82850.0, "30¢", "N/A", "Down")  # a second bot, same window
    with open(updown, newline="", encoding="utf-8") as f:
        assert len(list(csv.reader(f))) == 2


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


def combo_sim(tmp, strategy, **kwargs):
    return ComboSim("btc", 5, 30, 8, None, os.path.join(tmp, "trades.csv"), 100.0, "default", strategy=strategy,
                    **kwargs)


def regime_sim(tmp, **kwargs):
    return combo_sim(tmp, "regime", fav_max_move_pct=0, **kwargs)


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

    sim.strategy = "fav"
    sim.history = {START - 300: ["Down", True], START - 600: ["Up", True]}
    assert sim.regime_decision(START) == ("skip", "streak_1_down")
    sim.history = {START - 300: ["Up", False], START - 600: ["Up", False], START - 900: ["Down", True]}
    assert sim.regime_decision(START) == ("fav", "streak_2_up")


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


def test_against_strategy(tmp):
    sim = combo_sim(tmp, "against")
    three_up = {START - 300: ["Up", False], START - 600: ["Up", False], START - 900: ["Up", True],
                START - 1200: ["Down", True]}
    sim.history = dict(three_up)
    assert sim.regime_decision(START, None, 0.01) == ("skip", "streak_3_up_move_+0.010pct")
    assert sim.regime_decision(START, None, None) == ("skip", "prev_move_unknown")
    sim.history = {START - 300: ["Up", False], START - 600: ["Up", False], START - 900: ["Down", True]}
    assert sim.regime_decision(START, None, 0.08) == ("skip", "streak_2_up")

    sim.history = dict(three_up)
    state = sim.start_window(START, END, "s", "1", now=START, decision_pending=True)
    assert sim.regime_decision(START, None, -0.05) == ("against", "streak_3_up_move_-0.050pct")
    assert state.bet_side == "Down"
    sim.resolve_decision(state, None, mode="against")
    for offset, up, down in ((1, 0.53, 0.48), (290, 0.97, 0.04)):
        sim.on_tick(START + offset, up, down)
    state = sim.finish_window()
    assert state.fill_type == "only_down" and state.avg_price("Down") == 0.48 and state.stop_side is None
    row = sim.settle(state, "Down", 1, 2, None, taker_fee_bps=200)
    assert row["pnl"] == "4.0832"


def against_sim(tmp, **kwargs):
    kwargs.setdefault("min_move_vs_avg", 0)
    return AgainstSim("btc", 5, 30, 8, None, os.path.join(tmp, "trades.csv"), 100.0, "default", **kwargs)


def up_structure_moves(n=14):
    """Moves that make an up structure with RSI agreeing (upish). Avg abs move ~0.05%."""
    pattern = [-0.05, 0.05, -0.05, 0.05, 0.05, 0.05, -0.05, 0.05, 0.05, 0.05, 0.05, 0.05]
    while len(pattern) < n:
        pattern = [0.05] + pattern
    return {START - i * 300: m for i, m in zip(range(n, 0, -1), pattern[-n:])}


def test_against_move_vs_average(tmp):
    sim = against_sim(tmp, min_move_vs_avg=1.0, avg_windows=12, rsi_windows=4)
    sim.history = {START - 300: ["Up", False], START - 600: ["Up", False], START - 900: ["Up", True],
                   START - 1200: ["Down", True]}
    state = sim.start_window(START, END, "s", "1", now=START, decision_pending=True)
    assert sim.regime_decision(START, None, 0.05) == (
        "skip", "filter_struct_rsi_streak_3_up_move_+0.050pct_avg_move_unknown")
    sim.moves = up_structure_moves(12)
    # Up streak with up structure is the trending side → skip; need a Down streak to bet
    assert sim.regime_decision(START, None, 0.05) == (
        "skip", "filter_struct_rsi_streak_3_up_move_+0.050pct_1.00x_avg_structure_up_rsi_100_with_structure")
    assert round(state.avg_move_pct, 6) == 0.05 and round(state.move_vs_avg, 6) == 1.0 and state.structure == "up"
    sim.history = {START - 300: ["Down", False], START - 600: ["Down", False], START - 900: ["Down", True],
                   START - 1200: ["Up", True]}
    assert sim.regime_decision(START, None, -0.05) == (
        "against", "filter_struct_rsi_streak_3_down_move_-0.050pct_1.00x_avg_structure_up_rsi_75")
    assert state.bet_side == "Up" and state.rsi is not None
    sim.moves[START - 300] = -0.02  # last move now under the average
    assert sim.regime_decision(START, None, None)[0] == "skip"
    assert sim.regime_decision(START, None, -0.02) == (
        "skip", "filter_struct_rsi_streak_3_down_move_-0.020pct_0.42x_avg")
    assert round(state.move_vs_avg, 2) == 0.42
    row = sim.settle(sim.finish_window(), "Up", 1, 2, None)
    assert row["move_vs_avg"] == "0.42" and row["structure"] == "up" and row["rsi_14"]


def test_against_exactly_three_and_take_profit(tmp):
    sim = against_sim(tmp, avg_windows=12)
    three_up = {START - 300: ["Up", False], START - 600: ["Up", False], START - 900: ["Up", True],
                START - 1200: ["Down", True]}
    sim.history = dict(three_up)
    sim.moves = up_structure_moves()
    three_down = {START - 300: ["Down", False], START - 600: ["Down", False], START - 900: ["Down", True],
                  START - 1200: ["Up", True]}
    sim.history = dict(three_down)
    assert sim.regime_decision(START, None, None) == ("skip", "prev_move_unknown")
    state = sim.start_window(START, END, "s", "1", now=START, decision_pending=True)
    # Down streak against up structure + RSI agree → buy Up (move vs avg is logged, not required)
    assert sim.regime_decision(START, None, -0.01) == (
        "against", "filter_struct_rsi_streak_3_down_move_-0.010pct_0.21x_avg_structure_up_rsi_76")
    assert sim.regime_decision(START, None, -0.05) == (
        "against", "filter_struct_rsi_streak_3_down_move_-0.050pct_1.00x_avg_structure_up_rsi_71")
    assert state.bet_side == "Up" and state.take_profit == 0
    # the streak kept going: skipped until it breaks
    sim.history[START] = ["Down", False]
    assert sim.regime_decision(START + 300, None, -0.08) == ("skip", "streak_4_down_not_broken")
    sim.history[START + 300] = ["Up", False]
    assert sim.regime_decision(START + 600, None, 0.08) == ("skip", "streak_1_up")

    sim.take_profit_all = True
    sim.history = dict(three_down)
    sim.moves = up_structure_moves()
    state = sim.start_window(START, END, "s", "1", now=START, decision_pending=True)
    assert sim.regime_decision(START, None, -0.05) == (
        "against", "filter_struct_rsi_streak_3_down_move_-0.050pct_1.00x_avg_structure_up_rsi_71_tp_85c")
    assert state.take_profit == 0.85 and state.bet_side == "Up"
    sim.resolve_decision(state, None, mode="against")
    for offset, up, down in ((1, 0.48, 0.53), (100, 0.86, 0.16), (150, 0.87, 0.14), (290, 0.04, 0.97)):
        sim.on_tick(START + offset, up, down)
    state = sim.finish_window()
    assert state.tp_side == "Up" and state.tp_contracts == 8 and state.tp_time_left == 150
    assert state.held("Up") == 0 and "take-profit" in sim.orders_text(state)
    row = sim.settle(state, "Down", 1, 2, None, taker_fee_bps=200)
    assert row["pnl"] == "2.8832" and row["take_profit_cents"] == 85 and row["tp_contracts"] == 8

    sim.take_profit_cents = 0
    sim.history = dict(three_down)
    sim.moves = up_structure_moves()
    state = sim.start_window(START, END, "s", "1", now=START, decision_pending=True)
    assert sim.regime_decision(START, None, -0.05) == (
        "against", "filter_struct_rsi_streak_3_down_move_-0.050pct_1.00x_avg_structure_up_rsi_71")
    assert state.take_profit == 0


def test_against_structure(tmp):
    sim = against_sim(tmp, avg_windows=12)
    three_up = {START - 300: ["Up", False], START - 600: ["Up", False], START - 900: ["Up", True],
                START - 1200: ["Down", True]}
    sim.history = dict(three_up)
    state = sim.start_window(START, END, "s", "1", now=START, decision_pending=True)
    assert sim.regime_decision(START, None, 0.05) == (
        "skip", "filter_struct_rsi_streak_3_up_move_+0.050pct_structure_unknown")
    # Up streak with up structure → trending side → skip
    sim.moves = up_structure_moves()
    assert sim.regime_decision(START, None, 0.05) == (
        "skip", "filter_struct_rsi_streak_3_up_move_+0.050pct_1.00x_avg_structure_up_rsi_79_with_structure")
    assert state.structure == "up"
    # Down streak against an up structure + RSI agree → against (buy Up)
    sim.history = {START - 300: ["Down", False], START - 600: ["Down", False], START - 900: ["Down", True],
                   START - 1200: ["Up", True]}
    sim.moves = up_structure_moves()
    assert sim.regime_decision(START, None, -0.05) == (
        "against", "filter_struct_rsi_streak_3_down_move_-0.050pct_1.00x_avg_structure_up_rsi_71")
    assert state.bet_side == "Up"
    # choppy moves → mixed → skip
    sim.history = dict(three_up)
    sim.moves = {START - i * 300: (0.05 if i % 2 else -0.05) for i in range(1, 13)}
    assert sim.regime_decision(START, None, 0.05) == (
        "skip", "filter_struct_rsi_streak_3_up_move_+0.050pct_1.00x_avg_structure_mixed")
    assert state.structure == "mixed"


def test_against_rsi_agree(tmp):
    """Against requires RSI to agree with structure (up>55 / down<45); mid and disagree skip."""
    three_down = {START - 300: ["Down", False], START - 600: ["Down", False], START - 900: ["Down", True],
                  START - 1200: ["Up", True]}

    # RSI unknown: enough moves for structure, not for default rsi_windows=14
    sim = against_sim(tmp, avg_windows=12)
    sim.history = dict(three_down)
    state = sim.start_window(START, END, "s", "1", now=START, decision_pending=True)
    sim.moves = up_structure_moves(12)
    assert sim.regime_decision(START, None, -0.05) == (
        "skip", "filter_struct_rsi_streak_3_down_move_-0.050pct_1.00x_avg_structure_up_rsi_unknown")
    assert state.rsi is None and state.structure == "up"

    # mid / disagree / agree with a short RSI window so fixtures control the lean
    sim = against_sim(tmp, avg_windows=12, rsi_windows=4)
    sim.history = dict(three_down)
    state = sim.start_window(START, END, "s", "1", now=START, decision_pending=True)

    # last window is overwritten to prev_move (-0.05); balance the other three for RSI ~50
    sim.moves = up_structure_moves(12)
    sim.moves[START - 600] = 0.05
    sim.moves[START - 900] = 0.05
    sim.moves[START - 1200] = -0.05
    assert sim.regime_decision(START, None, -0.05) == (
        "skip", "filter_struct_rsi_streak_3_down_move_-0.050pct_1.00x_avg_structure_up_rsi_50_rsi_mid")
    assert round(state.rsi) == 50

    sim.moves = up_structure_moves(12)
    for i in range(1, 5):
        sim.moves[START - i * 300] = -0.05
    assert sim.regime_decision(START, None, -0.05) == (
        "skip", "filter_struct_rsi_streak_3_down_move_-0.050pct_1.00x_avg_structure_up_rsi_0_rsi_disagree")
    assert state.rsi == 0.0

    sim.moves = up_structure_moves(12)
    assert sim.regime_decision(START, None, -0.05) == (
        "against", "filter_struct_rsi_streak_3_down_move_-0.050pct_1.00x_avg_structure_up_rsi_75")
    assert state.bet_side == "Up"


def test_against_filter_bare_rsi_and_struct(tmp):
    """bare / rsi / struct-only filters."""
    three_up = {START - 300: ["Up", False], START - 600: ["Up", False], START - 900: ["Up", True],
                START - 1200: ["Down", True]}
    three_down = {START - 300: ["Down", False], START - 600: ["Down", False], START - 900: ["Down", True],
                  START - 1200: ["Up", True]}

    bare = against_sim(tmp, avg_windows=12, against_filter="bare")
    bare.history = dict(three_up)
    state = bare.start_window(START, END, "s", "1", now=START, decision_pending=True)
    # with-structure would skip on struct_rsi; bare still takes
    bare.moves = up_structure_moves()
    assert bare.regime_decision(START, None, 0.05) == (
        "against", "filter_bare_streak_3_up_move_+0.050pct_1.00x_avg_structure_up_rsi_79")
    assert state.bet_side == "Down"
    bare.history[START] = ["Up", False]
    assert bare.regime_decision(START + 300, None, 0.05) == ("skip", "streak_4_up_not_broken")

    rsi = against_sim(tmp, avg_windows=12, rsi_windows=4, against_filter="rsi")
    rsi.history = dict(three_down)
    state = rsi.start_window(START, END, "s", "1", now=START, decision_pending=True)
    rsi.moves = up_structure_moves(12)
    # fade Down → bet Up, RSI high → with trend → take (no structure required)
    assert rsi.regime_decision(START, None, -0.05) == (
        "against", "filter_rsi_streak_3_down_move_-0.050pct_1.00x_avg_structure_up_rsi_75")
    assert state.bet_side == "Up"
    rsi.history = dict(three_up)
    # fade Up → bet Down while RSI high → against RSI trend → skip
    assert rsi.regime_decision(START, None, 0.05) == (
        "skip", "filter_rsi_streak_3_up_move_+0.050pct_1.00x_avg_structure_up_rsi_100_rsi_against_trend")

    # struct only: against clear structure, no RSI gate (mid/disagree still take)
    st = against_sim(tmp, avg_windows=12, rsi_windows=4, against_filter="struct")
    st.history = dict(three_down)
    state = st.start_window(START, END, "s", "1", now=START, decision_pending=True)
    st.moves = up_structure_moves(12)
    for i in range(1, 5):
        st.moves[START - i * 300] = -0.05  # RSI disagree with up structure
    assert st.regime_decision(START, None, -0.05) == (
        "against", "filter_struct_streak_3_down_move_-0.050pct_1.00x_avg_structure_up_rsi_0")
    assert state.bet_side == "Up"
    st.history = dict(three_up)
    st.moves = up_structure_moves(12)
    assert st.regime_decision(START, None, 0.05) == (
        "skip", "filter_struct_streak_3_up_move_+0.050pct_1.00x_avg_structure_up_rsi_100_with_structure")


def test_combo_strategy(tmp):
    sim = combo_sim(tmp, "combo", trend_windows=0)
    sim.history = {START - 300: ["Up", False], START - 600: ["Up", False], START - 900: ["Down", True]}
    assert sim.regime_decision(START, None, 0.01) == ("skip", "streak_2_up")
    sim.history = {START - 300: ["Up", False], START - 600: ["Up", False], START - 900: ["Up", True],
                   START - 1200: ["Down", True]}
    assert sim.regime_decision(START, None, 0.08) == ("against", "streak_3_up_move_+0.080pct")
    assert sim.regime_decision(START, None, 0.01) == ("skip", "streak_3_up_move_+0.010pct")
    sim.history = {START - 300: ["Up", False], START - 600: ["Down", True]}
    assert sim.regime_decision(START, None, 0.08) == ("skip", "streak_1_up")


def test_combo_continue(tmp):
    sim = combo_sim(tmp, "combo", trend_windows=0)
    three_up = {START - 300: ["Up", False], START - 600: ["Up", False], START - 900: ["Up", True],
                START - 1200: ["Down", True]}
    sim.history = dict(three_up)
    sim.moves = {START - 600: 0.04}
    state = sim.start_window(START, END, "s", "1", now=START, decision_pending=True)
    assert sim.regime_decision(START, None, 0.03) == ("continue", "streak_3_up_move_+0.030pct_slowing_from_+0.040pct")
    assert state.bet_side == "Up" and sim.moves[START - 300] == 0.03
    sim.moves[START - 600] = 0.02
    assert sim.regime_decision(START, None, 0.03) == ("against", "streak_3_up_move_+0.030pct")
    sim.moves[START - 600] = 0.2
    assert sim.regime_decision(START, None, 0.06) == ("against", "streak_3_up_move_+0.060pct")
    sim.moves[START - 600] = 0.04
    assert sim.regime_decision(START, None, 0.01)[0] == "continue"
    sim.continue_max_move_pct = 0
    assert sim.regime_decision(START, None, 0.01) == ("skip", "streak_3_up_move_+0.010pct")
    sim.continue_max_move_pct = 0.05

    state.action, state.action_reason = sim.regime_decision(START, None, 0.03)
    sim.resolve_decision(state, None, mode="continue")
    for offset, up, down in ((1, 0.55, 0.46), (290, 0.97, 0.04)):
        sim.on_tick(START + offset, up, down)
    state = sim.finish_window()
    assert state.fill_type == "only_up" and state.avg_price("Up") == 0.55
    assert sim.orders_text(state) == "with streak Up 8 @ 55¢"
    row = sim.settle(state, "Up", 100.0, 100.05, None, taker_fee_bps=200)
    assert row["action"] == "continue" and abs(sim.moves[START] - 0.05) < 1e-9

    against = combo_sim(tmp, "against")
    against.history, against.moves = dict(three_up), {START - 600: 0.04}
    assert against.regime_decision(START, None, 0.03) == ("against", "streak_3_up_move_+0.030pct")


def test_combo_trend(tmp):
    sim = combo_sim(tmp, "combo", trend_windows=12)
    # 12 windows before the streak: Up 3, Down 1, Up 4, Down 2, Up 2 -> 2 Up streaks v 0 Down
    before = ["Up"] * 3 + ["Down"] + ["Up"] * 4 + ["Down"] * 2 + ["Up"] * 2

    def setup(streak_side, n=3):
        streak_start = START - n * 300
        sim.history = {streak_start - (12 - i) * 300: [o, False] for i, o in enumerate(before)}
        sim.history[streak_start - 300] = ["Down" if streak_side == "Up" else "Up", False]
        sim.history.update({streak_start + i * 300: [streak_side, False] for i in range(n)})

    assert sim.trend_before(START - 900) is None
    setup("Down")
    assert sim.trend_before(START - 900) == ("Up", 2, 0)
    state = sim.start_window(START, END, "s", "1", now=START, decision_pending=True)
    tag = "trend_up_2v0_against_trend"
    assert sim.regime_decision(START, None, -0.03) == ("skip", f"streak_3_down_move_-0.030pct_{tag}")
    assert sim.regime_decision(START, None, -0.06) == ("against", f"streak_3_down_move_-0.060pct_{tag}")
    assert state.bet_side == "Up"
    # the streak kept going: no second bet against it, unless the stop is off
    sim.history[START] = ["Down", False]
    assert sim.regime_decision(START + 300, None, -0.08) == (
        "skip", f"streak_4_down_move_-0.080pct_{tag}_lost_on_this_streak")
    sim.against_stop_after_loss = False
    assert sim.regime_decision(START + 300, None, -0.08)[0] == "against"
    sim.against_stop_after_loss = True

    setup("Up")
    tag = "trend_up_2v0_with_trend"
    assert sim.regime_decision(START, None, 0.01) == ("skip", f"streak_3_up_move_+0.010pct_{tag}")  # move before unknown
    sim.moves[START - 600] = 0.04
    assert sim.regime_decision(START, None, 0.03) == ("continue", f"streak_3_up_move_+0.030pct_{tag}_slowing_from_+0.040pct")
    assert state.bet_side == "Up"
    assert sim.regime_decision(START, None, 0.045) == ("skip", f"streak_3_up_move_+0.045pct_{tag}")
    assert sim.regime_decision(START, None, 0.07) == ("skip", f"streak_3_up_move_+0.070pct_{tag}")

    before[:] = ["Up"] * 3 + ["Down"] * 3 + ["Up", "Down"] * 3
    setup("Up")
    assert sim.trend_before(START - 900) == (None, 1, 1)
    assert sim.regime_decision(START, None, 0.03) == ("skip", "streak_3_up_move_+0.030pct_no_trend_1v1")
    sim.history.pop(START - 1500)
    assert sim.regime_decision(START, None, 0.03) == ("skip", "streak_3_up_move_+0.030pct_no_trend_1v1")
    sim.history.pop(START - 1800)
    assert sim.regime_decision(START, None, 0.03) == ("skip", "streak_3_up_move_+0.030pct_trend_unknown")


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
    assert loaded.moves == {START: 100.0, START + 300: 100.0}
    assert loaded.regime_decision(START + 600) == ("dual", "dual_in_last_3")


def test_load_updown_fills_streak_gaps(tmp):
    updown = os.path.join(tmp, "btc-5-updown.csv")
    with open(updown, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Time stamp", "price_to_beat", "final_price", "lowest_up", "lowest_down", "outcome"])
        w.writerow([START - 900, "$100.00", "$99.00", "10¢", "40¢", "Down"])
        w.writerow([START - 600, "$99.00", "$98.00", "5¢", "50¢", "Down"])
        w.writerow([START - 300, "$98.00", "$97.00", "8¢", "45¢", "Down"])
    sim = against_sim(tmp)
    sim.load_updown(updown)
    assert sim.history[START - 300][0] == "Down"
    assert sim._streak_before(START) == (3, "Down")
    assert round(sim.moves[START - 300], 4) == round((97 - 98) / 98 * 100, 4)


# Up's bid (1 - Down ask) first reaches 61¢ at 1:10 in; Up then dips to 51¢ (fills at 52¢), Down to 21¢ (fills at 25¢)
LEADER_TICKS = [(0, 0.50, 0.51), (30, 0.55, 0.46), (70, 0.62, 0.39), (100, 0.51, 0.50), (200, 0.80, 0.21),
                (250, 0.85, 0.16)]


def leader_sim(tmp):
    # stop-loss and cancel are on for the dual; the leader strategy ignores them
    return LeaderSim("btc", 5, 30, 8, None, os.path.join(tmp, "trades.csv"), 100.0, "default",
                     stop_loss_seconds=20, stop_loss_live=True)


def test_leader_bought_at_ask(tmp):
    sim = leader_sim(tmp)
    assert sim.watch_seconds() == (60, 120) and sim.order_cost() == 5.6
    assert sim.prev_move_skip_reason(0.12) is None and sim.prev_move_skip_reason(-0.10) is None
    assert sim.prev_move_skip_reason(0.05) == "prev_move_+0.050pct_calm"
    assert sim.prev_move_skip_reason(None) == "prev_move_unknown"
    # Up's bid reaches 61¢ at 1:10 in: 8 Up bought at once at its 62¢ ask, held through the dip and to the end
    state = run_window(sim, LEADER_TICKS)
    assert state.leader_side == "Up" and state.leader_time_left == 230 and state.placed_time_left == 230
    assert state.action == "leader" and state.action_reason == "leader_up_at_01:10_in", state.action_reason
    assert state.fill_type == "only_up" and state.up_fill_price == 0.62 and state.up_taker
    assert state.up_fill_time_left == 230 and state.contracts("Up") == 8
    assert state.hedge_order is None and state.cancelled_time_left is None and state.stop_side is None
    row = sim.settle(state, "Up", 82900.0, 82950.0, 0.02, taker_fee_bps=200)
    assert row["pnl"] == "2.9408" and row["leader_side"] == "Up" and row["limit_cents"] == "ask<=70", row
    state = run_window(sim, LEADER_TICKS)
    assert sim.compute_result(state, "Down", taker_fee_bps=200)[1] == -5.0592


def test_leader_skips(tmp):
    sim = leader_sim(tmp)
    # Up reached 60¢ before 1:00 in: not a leader; nothing else by 2:00 in
    state = run_window(sim, [(0, 0.50, 0.51), (30, 0.65, 0.36), (70, 0.66, 0.35), (125, 0.70, 0.31)])
    assert not state.trading and not state.orders and state.action_reason == "no_leader"
    assert state.notes == ["no_leader_by_02:00_in", "not_traded"], state.notes
    assert sim.settle(state, "Down", 1, 2, None)["pnl"] == "0.0000"
    # Up early, then Down reaches 60¢ inside the watch: Down is the leader
    state = run_window(sim, [(0, 0.50, 0.51), (30, 0.65, 0.36), (90, 0.38, 0.63)])
    assert state.leader_side == "Down" and state.leader_time_left == 210 and state.down_fill_price == 0.63
    # leader's ask over the 70¢ cap (wide spread): skipped
    state = run_window(sim, [(0, 0.50, 0.51), (80, 0.40, 0.75)])
    assert state.leader_side == "Down" and not state.trading and not state.orders
    assert state.action_reason == "leader_down_ask_75c"
    # calm last window
    state = sim.start_window(START, END, "s", "1", now=START, decision_pending=True)
    sim.resolve_decision(state, sim.prev_move_skip_reason(0.03))
    sim.on_tick(START + 70, 0.62, 0.39)
    assert not state.trading and not state.orders and state.mode == "leader"
    assert state.action == "skip" and state.action_reason == "prev_move_+0.030pct_calm"


def main():
    with tempfile.TemporaryDirectory() as tmp:
        for test in (
            test_both_fill_and_lowest_times,
            test_one_side_win_and_loss,
            test_no_fill_and_unknown,
            test_place_at_and_taker_fill,
            test_quiet_dual_rest_both,
            test_dual_skew_lift_and_other,
            test_dual_skew_strand_exit,
            test_dual_waits_when_delta_loud,
            test_quiet_dual_waits_for_enter_after,
            test_scalp_tp,
            test_scalp_ignores_brief_wick,
            test_scalp_confirmed_stop,
            test_scalp_time_exit,
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
            test_btc_delta_changes_on_change_only,
            test_tie,
            test_log_formats_and_capital_restore,
            test_old_log_upgraded_in_place,
            test_regime_decision,
            test_regime_favourite_window,
            test_against_strategy,
            test_against_exactly_three_and_take_profit,
            test_against_structure,
            test_against_rsi_agree,
            test_against_move_vs_average,
            test_combo_strategy,
            test_combo_continue,
            test_combo_trend,
            test_regime_history_from_log,
            test_load_updown_fills_streak_gaps,
            test_leader_bought_at_ask,
            test_leader_skips,
        ):
            test(tempfile.mkdtemp(dir=tmp))
            print(f"✅ {test.__name__}")
        test_book_and_slugs()
        print("✅ test_book_and_slugs")
    print("All tests passed")


if __name__ == "__main__":
    main()
