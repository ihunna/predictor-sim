# Decision log — scalp

## 2026-10-29 — IMPLEMENTED: ATR + velocity + adverse Δ pipeline

After time gate + ask ≤30¢ (side chosen), ordered entry gates (all durations):

1. **ATR24h ≤ $350** (`--scalp-atr-max`; 0 = off) — Binance `BTCUSDT` 1h mean(high−low) over 24 completed hours. Fail closed if ATR missing. **(1B)** skip only current window entries; re-check next window (no day-long pause).
2. **Adverse velocity ≤ $60/min** (`--scalp-velocity-max`; 0 = off) — `adverse_usd / max(elapsed_s,1)/60`. Favorable/flat (`adverse ≤ 0`) always passes. **(2A)** same for 5m/15m/60m.
3. **Signed adverse Δ ≤ cap** (`--scalp-delta-cap`) — $20 / $50 / $200 by duration (unchanged).

Skip reasons logged: `atr_high`, `velocity_high`, `adverse_delta`.

## 2026-10-29 — IMPLEMENTED: full formula all durations (30→TP60 + signed Δ)

Live scalp defaults (same formula, scaled by duration):

| Duration | Warm-up | Entry window (elapsed) | Δ cap (signed adverse) | Entry | TP |
|----------|---------|------------------------|------------------------|-------|-----|
| 5m | 0–5s | 5s–30s inclusive | $20 | ask ≤30¢ taker | limit 60¢ maker |
| 15m | 0–15s | 15s–120s inclusive | $50 | ask ≤30¢ taker | limit 60¢ maker |
| 60m | 0–60s | 60s–300s inclusive | $200 | ask ≤30¢ taker | limit 60¢ maker |

- **Split / ladder off** by default (`--scalp-split-entry 0`, `--scalp-ladder-first 0`).
- **Signed adverse Δ:** `±(btc_move_pct/100)×PTB` against the side we would buy; skip if `> cap`.
- **TP** is a resting limit (maker fee path in `compute_result`); entry lifts the ask (taker).
- Miss TP → hold to settle.
- Vol gates (ATR / velocity) layered on top — see entry above.

Replay (60m, touch-based, maker TP / taker entry fees): Δ filter improves TP60; TP70 scored higher on that sample but live default stays **TP60** unless `--scalp-target 70`.

## 2026-10-21 — IMPLEMENTED (superseded): TP60 + split + Δ caps

Prior live defaults (split 20→40 on 5m/15m, first-third cancel). Replaced by 2026-10-29.


## 2026-10-20 — Skip scalp fades of a hard 60m hour (>$150)?

**Status: recorded as user preference — NOT implemented in live code. Re-review on fresh data.**
**Code name (when implemented): `--scalp-skip-hour-fade 150`** (argparse dest `scalp_skip_hour_fade`, USD threshold, 0/off by default).

### The rule in question
On 5m and 15m scalp, skip the entry when the scalp buys the side *opposite* the current
60m window's direction and the 60m move from its open is large:

- Hour is up > $150 from its open → do **not** buy Down
- Hour is down > $150 from its open → do **not** buy Up

(60m delta measured at the 5m/15m window start: window start price vs current hour's open.)

### User's position
Even though the replay says these trades are profitable, the user judges skipping them
the better option — they usually lose directionally, and the user does not want to fade a
side the 60m window is winning hard. Profit forgone is accepted.

### Replay evidence at time of decision (ladder 40/60, savior off, CF gaps excluded)
Data: scalp logs through 2026-10-20 ~07:55 UTC. Dropped = window not traded.

| | Take anything | Skip >$150 fades | Skipped set |
|---|---|---|---|
| 5m | n=469, EV +$0.252, +$118.08 | n=420, EV +$0.228, +$95.84 | n=49, EV +$0.454, +$22.24 |
| 15m | n=168, EV +$0.307, +$51.60 | n=154, EV +$0.253, +$38.96 | n=14, EV +$0.903, +$12.64 |
| Pooled | n=637, EV +$0.266, +$169.68 | n=574, EV +$0.235, +$134.80 | n=63, EV +$0.554, +$34.88 |

Risk profile, pooled: skipping gave SD $1.61 vs $1.60, max DD −$27.92 vs −$25.12,
worst trade −$2.40 unchanged, loss streak 14 unchanged, profit factor 1.37 vs 1.43.
So the replay found **no smoothness benefit** — only fewer trades (574 vs 637) and
less capital deployed ($1,328 vs $1,474).

Why the fades still paid on replay: the faded side wins the window only ~35% of the time
on 5m, but reaches the 40¢ ladder rung ~88% of the time. The rule only looks good under
hold-to-settle (e.g. hour down <−$150, buy Up: settle EV −$0.18 vs ladder EV +$0.37).

### Review trigger
Re-run this exact test when at least ~100 new pooled >$150-fade fills exist
(roughly another week of 5m+15m scalp logs), or sooner if live skipped-shadow trades
can be logged. Revisit if, on the fresh sample, the skipped set turns EV-negative on
the ladder replay or skipping starts improving max drawdown.

### Correction 2026-10-21 (later same day)
The replay helper `tp_point.max_bid_after_fill` was found to count bid rises that
occurred *before* the fill (helper since fixed: post-fill rises only). All EVs in the
two sections above are inflated. Corrected re-run, same sample: the skipped
(>$150 fade) set is n=92, EV **+$0.285** (not +$0.554/+$0.590) and skipping still
lowers kept EV (−$0.161 → −$0.211 on 5+15m). Qualitative conclusion unchanged —
skipping still costs — but magnitudes are roughly half those recorded above.

### Re-check 2026-10-21 (data through 2026-10-21 08:30 UTC)
Skipped set grew to n=92 pooled and got *stronger*, not weaker:
5m dropped n=73, EV +$0.460 (+$33.60); 15m dropped n=19, EV +$1.086 (+$20.64);
pooled dropped EV +$0.590 (+$54.24). Skipping would cut pooled EV +$0.313 → +$0.283
and worsen max DD (−$23.28 → −$26.32). Conclusion unchanged; user preference stands
as recorded. Not yet at the ~100-new-fills trigger (only ~29 new fade fills since
the original test), so this is an interim check, not the formal review.
