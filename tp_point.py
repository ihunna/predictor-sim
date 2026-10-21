#!/usr/bin/env python3
"""tp-point — scalp take-profit level / ladder judgment tables.

Re-run after pulling fresh ``btc-{5,15,60}-predict-scalp-trades.csv``::

    python tp_point.py

Sample (live-matched, same for every compare):
  - Scalp fills only (action=scalp, only_up / only_down)
  - From first 60m scalp fill window onward (align 5m/15m to that start)
  - Drop old hard-stop exits and TP30-era rows
  - Entry = logged avg cents; size = logged contracts (default 8)

Max bid after fill (conservative path for “would TP have hit?”):
  - ``{side}_max_bid_after_{30,20,15,10,5,40}c``
  - ``{side}_rise_{35..95}c_time_left`` present ⇒ bid reached that level
  - Ask snaps after fill: bid ≈ 1 − other_ask

PnL formulas:
  - **Single TPx:** if max_bid ≥ x → (x − entry)/100 × n; else logged settle pnl
  - **Ladder:** split size across rungs (equal or weights); each rung sells if
    max_bid ≥ rung at (rung − entry)/100 × size; unsold contracts settle from
    outcome (win side → 1−entry, lose → −entry, tie → 0.5−entry)

Baseline row is always **TP60 logged (actual)** sum of ``pnl`` in the sample.
Simulated TP60 can differ when path max_bid ≥ 60 but the bot did not TP.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

BOT = Path(__file__).resolve().parent


def parse_ask(x):
    if not x or str(x).strip().lower() in ("", "none", "n/a"):
        return None
    try:
        v = float(str(x).strip().replace("¢", "").replace("$", ""))
    except (TypeError, ValueError):
        return None
    if v > 1.5:
        v /= 100
    if v <= 0 or v >= 1:
        return None
    return round(v, 4)


def cents(x):
    a = parse_ask(x)
    return int(round(a * 100)) if a is not None else None


def secs(x):
    if not x or str(x).strip().lower() in ("", "none"):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def max_bid_after_fill(r, side):
    s = side.lower()
    best = None
    fill_left = secs(r.get(f"{s}_fill_time_left")) or secs(r.get(f"{s}_touch_30c_time_left"))
    # Buckets at/below the 30c entry touch only: max_bid_after_40c can pre-date the
    # fill (ask fell through 40 before we bought at <=30), so it must not count.
    for bucket in (30, 20, 15, 10, 5):
        mb = cents(r.get(f"{s}_max_bid_after_{bucket}c"))
        if mb is not None:
            best = mb if best is None else max(best, mb)
    # Rise levels count only if reached at/after the fill (time_left <= fill_left);
    # a rise earlier in the window happened before we owned the side.
    for lvl in range(35, 100, 5):
        tl = secs(r.get(f"{s}_rise_{lvl}c_time_left"))
        if tl is not None and (fill_left is None or tl <= fill_left + 1e-9):
            best = lvl if best is None else max(best, lvl)
    try:
        dur = int(float(r.get("duration") or 5))
    except (TypeError, ValueError):
        dur = 5
    win = dur * 60
    times = set()
    for k in r:
        if k.startswith("up_ask_") and k.endswith("s_in"):
            try:
                times.add(int(k[len("up_ask_") : -len("s_in")]))
            except ValueError:
                pass
    for t in sorted(times):
        remaining = win - t
        if fill_left is not None and remaining > fill_left + 1e-9:
            continue
        u, d = parse_ask(r.get(f"up_ask_{t}s_in")), parse_ask(r.get(f"down_ask_{t}s_in"))
        if not u or not d:
            continue
        bid = (1 - d) if side == "Up" else (1 - u)
        bc = int(round(bid * 100))
        best = bc if best is None else max(best, bc)
    return best


def cutoff_60m_start():
    starts = []
    path = BOT / "btc-60-predict-scalp-trades.csv"
    if not path.is_file():
        return None
    for r in csv.DictReader(path.open()):
        if (r.get("action") or "").strip() != "scalp":
            continue
        if (r.get("fill_type") or "").strip() not in ("only_up", "only_down"):
            continue
        try:
            starts.append(int(float(r["window_start"])))
        except (TypeError, ValueError):
            continue
    return min(starts) if starts else None


def load_fills(dur, cutoff):
    path = BOT / f"btc-{dur}-predict-scalp-trades.csv"
    if not path.is_file():
        return []
    out = []
    for r in csv.DictReader(path.open()):
        try:
            ws = int(float(r["window_start"]))
            pnl = float(r.get("pnl") or "")
        except (TypeError, ValueError):
            continue
        if cutoff is not None and ws < cutoff:
            continue
        if (r.get("action") or "").strip() != "scalp":
            continue
        fill = (r.get("fill_type") or "").strip()
        if fill not in ("only_up", "only_down"):
            continue
        notes = r.get("notes") or ""
        if "stop_sold_" in notes and "held_" in notes:
            continue
        reason = r.get("action_reason") or ""
        if "tp_30" in reason or (r.get("take_profit_cents") or "").strip() == "30":
            continue
        side = "Up" if fill == "only_up" else "Down"
        try:
            entry = int(float(r.get(f"{side.lower()}_avg_cents") or 0))
        except (TypeError, ValueError):
            entry = 0
        if entry <= 0:
            continue
        try:
            n = int(float(r.get(f"{side.lower()}_contracts") or r.get("contracts") or 8))
        except (TypeError, ValueError):
            n = 8
        out.append(
            {
                "entry": entry,
                "n": n,
                "side": side,
                "outcome": (r.get("outcome") or "").strip(),
                "actual_pnl": pnl,
                "max_bid": max_bid_after_fill(r, side),
            }
        )
    return out


def settle_per(entry, side, outcome):
    if outcome == side:
        return 1.0 - entry / 100.0
    if outcome == "Tie":
        return 0.5 - entry / 100.0
    return -entry / 100.0


def split_sizes(n, weights):
    wsum = sum(weights)
    raw = [n * w / wsum for w in weights]
    sizes = [int(x) for x in raw]
    rem = n - sum(sizes)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - sizes[i], reverse=True)
    for i in range(rem):
        sizes[order[i % len(sizes)]] += 1
    return sizes


def single_tp(fills, tp):
    total = wins = losses = 0
    for f in fills:
        if f["max_bid"] is not None and f["max_bid"] >= tp:
            pnl = (tp - f["entry"]) / 100.0 * f["n"]
        else:
            pnl = f["actual_pnl"]
        total += pnl
        wins += pnl > 0
        losses += pnl < 0
    return total, wins, losses


def ladder_tp(fills, rungs, weights=None):
    total = wins = losses = 0
    for f in fills:
        entry, n, mb = f["entry"], f["n"], f["max_bid"]
        if weights is None:
            sizes = split_sizes(n, [1] * len(rungs))
        elif len(weights) == len(rungs) + 1:
            parts = split_sizes(n, weights)
            sizes = parts[:-1]
        else:
            sizes = split_sizes(n, weights)
        pnl = 0.0
        sold = 0
        for lvl, sz in zip(rungs, sizes):
            if sz <= 0:
                continue
            if mb is not None and mb >= lvl:
                pnl += (lvl - entry) / 100.0 * sz
                sold += sz
        rem = n - sold
        if rem > 0:
            pnl += settle_per(entry, f["side"], f["outcome"]) * rem
        total += pnl
        wins += pnl > 0
        losses += pnl < 0
    return total, wins, losses


VARIANTS = [
    ("TP60 all-in (sim)", "s", 60),
    ("TP50 all-in (sim)", "s", 50),
    ("TP40 all-in (sim)", "s", 40),
    ("⅓ each @40 / 50 / 60", "l", [40, 50, 60], None),
    ("½ @40 + ½ @60", "l", [40, 60], None),
    ("½ @50 + ½ @60", "l", [50, 60], None),
    ("⅕ each 40→60 step 5¢", "l", [40, 45, 50, 55, 60], None),
    ("5¢ ladder 2× size 40→60", "l", [40, 45, 50, 55, 60], [1, 2, 4, 8, 16]),
    ("⅓ @40 + ⅓ @50 + ⅓ hold", "l", [40, 50], [1, 1, 1]),
    ("½ @40 + ½ hold", "l", [40], [1, 1]),
]


def print_table(dur, fills, act, aw, al):
    n = len(fills)
    print(f"\n### {dur}m  n={n}  (from 60m start)\n")
    print("| strategy | wins | losses | WR | net | vs actual |")
    print("|---|---:|---:|---:|---:|---:|")
    print(f"| **TP60 logged (actual)** | {aw} | {al} | {100 * aw / n:.0f}% | ${act:+.2f} | — |")
    for v in VARIANTS:
        name, kind = v[0], v[1]
        if kind == "s":
            total, w, l = single_tp(fills, v[2])
        else:
            total, w, l = ladder_tp(fills, v[2], v[3])
        print(f"| {name} | {w} | {l} | {100 * w / n:.0f}% | ${total:+.2f} | ${total - act:+.2f} |")


def main():
    cutoff = cutoff_60m_start()
    if cutoff is None:
        print("No 60m scalp fills found; pull btc-60-predict-scalp-trades.csv first.", file=sys.stderr)
        return 1
    print(f"tp-point  cutoff_ws={cutoff}  (first 60m scalp fill)")
    print("Sample: scalp fills from cutoff; no hard-stop / TP30 rows.")
    for dur in (60, 15, 5):
        fills = load_fills(dur, cutoff)
        if not fills:
            print(f"\n### {dur}m  n=0  (no fills)")
            continue
        act = sum(f["actual_pnl"] for f in fills)
        aw = sum(1 for f in fills if f["actual_pnl"] > 0)
        al = sum(1 for f in fills if f["actual_pnl"] < 0)
        print_table(dur, fills, act, aw, al)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
