# Decision log — scalp

Pending ideas (not defaults): see [`TO-BE-ADDED.md`](TO-BE-ADDED.md).

## 2026-11-11 — IMPLEMENTED: against exact streak N=2

Against default `--against-min-streak` **3→2** (exact-N fade). Live-matched ≤50¢ hold + `struct_rsi`:
N=2 n=133 WR56% EV+$0.60 Σ+$80 vs N=3 n=31 WR39% EV−$0.80 Σ−$25.

## 2026-11-11 — IMPLEMENTED: scalp TP≥90¢; against buy ≤50¢ hold

Scalp streak-fade: sell when bid ≥**90¢** (else hold). Against: buy ≤**50¢**, hold to settle
(default; `--take-profit-all` still optional). Aligns the two streak bots on price band + exit style.

## 2026-11-11 — IMPLEMENTED: streak-fade mid time gate 150/450/900 (current defaults)

Exact streak of **2** → buy other side ≤30¢; TP **90¢**; Δ $10/$20/$50; vel off; no latch.
Widened entry from 90/300/900 → **150/450/900** (live-matched: combined N=2 n=79→149,
EV +$1.81→+$1.61, Σ +$143→+$240). N=3/4 still worse. `--scalp-streak 0` = cheaper-side.

| Duration | Time gate | Velocity | Δ cap | TP |
|----------|-----------|----------|-------|----|
| 5m | ≤**150s** | **off** | $10 | **90¢** |
| 15m | ≤**450s** | **off** | $20 | **90¢** |
| 60m | ≤**900s** | **off** | $50 | **90¢** |

## 2026-11-11 — IMPLEMENTED: streak-fade as main scalp again

Exact-2 fade ≤30¢; hold; entry was ≤90/300/900; Δ on; vel off. Superseded same day by mid gate.

## 2026-11-11 — IMPLEMENTED: drop velocity window latch (all durations)

Live-matched since current filters (5m): among Δ+time-OK windows that vel blocked,
**latch-only** n=13 WR100% Σ+$31; entry-time vel only n=1 Σ−$2. Latch was leaving
cooled setups on the table. Velocity still checked **at entry** ($10/$20/$40); no
`velocity_latched` sticky skip. Notes: `velocity_high` only when entry tick is hot.

## 2026-11-10 — REVERT: cheaper-side as main scalp (current defaults)

Live-matched head-to-head: old cheaper-side (Δ + time + rising vel + TP) beat streak-fade
on total $ and sample size (combined Σ ~+$679 / n=569 vs streak-fade hold Σ ~+$132 / n=77).
Streak-fade stays optional via `--scalp-streak N`.

| Duration | Time gate | Velocity $/min | Δ cap | TP |
|----------|-----------|----------------|-------|----|
| 5m | ≤**150s** | **$10** | $10 | **60¢** |
| 15m | ≤**450s** | **$20** | $20 | **60¢** |
| 60m | ≤**900s** | **$40** | $50 | **70¢** |

## 2026-11-10 — TRIED then reverted: streak-fade as main scalp

Exact streak of **2** → buy other side ≤30¢; hold; entry 90/300/900; vel off. Higher EV/trade
on thin n; worse Σ than cheaper-side. Code kept behind `--scalp-streak`.

## 2026-11-10 — IMPLEMENTED: rising velocity $10/$20/$40

| Duration | Time gate | Velocity $/min | Δ cap |
|----------|-----------|----------------|-------|
| 5m | ≤150s | **$10** | $10 |
| 15m | ≤450s | **$20** | $20 |
| 60m | ≤900s | **$40** | $50 |

Longer windows allow faster adverse moves (more time to recover). Live-matched entry-time vel (no latch compare) under Δ+time: 5m WR 73%→76% EV+$1.20→+$1.31; 15m/60m ≈ unchanged. Latch still on when vel > 0. ATR/RSI off.

## 2026-11-09 — IMPLEMENTED: Δ + WR/PnL-balance time gates

| Duration | Lifespan | Time gate (elapsed) | ATR6h | Velocity | Δ cap |
|----------|----------|---------------------|-------|----------|-------|
| 5m | 300s | **≤150s** | **off** | (later $10) | **$10** |
| 15m | 900s | **≤450s** | **off** | (later $20) | **$20** |
| 60m | 3600s | **≤900s** | **off** | (later $40) | **$50** |

Live-matched balance pick (Binance Δ, TP else settle):

| | entry≤ | n | WR | EV | Σ |
|--|--:|--:|--:|--:|--:|
| 5m | **150s** | 1018 | 73% | +$1.20 | +$1219 |
| 15m | **450s** | 200 | 70% | +$0.94 | +$188 |
| 60m | **900s** | 22 | 59% | +$0.91 | +$20 |

## 2026-11-08 — IMPLEMENTED (superseded by Δ-only): ATR6h ≤ $500 + RSI readout

Live-matched on time+Δ (vel/RSI off unless noted); ~21% of windows have ATR≥$500:

| | time+Δ | +ATR≤500 | +RSI mix | +ATR≤500+RSI |
|--|--:|--:|--:|--:|
| 5m | n=689 EV+$1.17 Σ+$808 | n=589 EV+$1.07 Σ+$628 | n=168 EV+$1.25 Σ+$209 | n=146 EV+$1.11 Σ+$161 |
| 15m | n=392 EV+$0.28 Σ+$108 | n=356 EV+$0.21 Σ+$75 | n=124 EV+$0.43 Σ+$54 | n=115 EV+$0.36 Σ+$42 |
| 60m | n=84 EV+$0.61 Σ+$51 | n=63 EV+$0.81 Σ+$51 | n=22 EV+$0.94 Σ+$21 | n=16 EV+$1.49 Σ+$24 |

Note: on 5m/15m, fills *inside* ATR≥500 were historically +EV (5m n=100 EV+$1.80); ATR≤500 is a regime risk skip, not an EV max. On 60m ATR≥500 fills were flat — gate helps. RSI mix raises EV but cuts n ~3–4×; left **off** unless enabled.

Defaults then: `SCALP_ATR_MAX_USD` → **500** (later off under Δ-only).

## 2026-11-08 — IMPLEMENTED: time + Δ only (velocity off all durations)

Live-matched (lift ask → TP else settle; RSI/ATR off; pre-cheap vel latch when vel on):

| | time+vel+Δ | time+vel (no Δ) | **time+Δ (no vel)** |
|--|--:|--:|--:|
| 5m 10–120 | n=267 EV +$0.99 Σ+$265 | n=486 EV +$0.43 | **n=689 EV +$1.17 Σ+$808** |
| 15m 15–150 | n=60 EV +$0.51 | n=65 EV +$0.36 | n=392 EV +$0.28 Σ+$108 |
| 60m 61–300 | n=17 EV +$0.90 | n=17 (Δ rarely binds) | n=84 EV +$0.61 Σ+$51 |

Vel alone / no time gate was −EV. Defaults: `SCALP_VELOCITY_MAX_USD_PER_MIN` → **0** all durations. Keep Δ $10/$50/$200 + time gates. Re-enable vel with `--scalp-velocity-max` (prior 20/30/60).

## 2026-11-08 — IMPLEMENTED: drop RSI mix + 5m entry 10–120s

Live-matched (lift ask → TP else settle; vel latch + Δ on):

| | CURRENT (RSI on) | drop RSI | notes |
|--|--:|--:|--|
| 5m 10–90 | n=94 EV +$1.57 | n=366 EV +$1.44 | RSI was main volume choke |
| 5m time→120 + RSI | n=138 EV +$1.62 | — | time widen keeps EV |
| 15m | n=87 EV +$0.82 | n=255 EV +$0.50 | still +EV; do not chase >150s |
| 60m | n=17 EV +$1.68 | n=71 EV +$1.21 | late >300s no-RSI −EV — keep gate |

Defaults: `SCALP_RSI_GOOD_MIX_DEFAULT` → **False**; `SCALP_ENTRY_ELAPSED[5]` 90→**120** (cancel-left → 180s). Leave 15m/60m time, Δ, vel latch unchanged. `--scalp-rsi-mix` to re-enable.

## 2026-11-08 — IMPLEMENTED: 5m entry gate 10–90s (superseded → 120)

Live-matched 5m (30¢, vel+latch, Δ, RSI mix): 10–45s n=44 WR84% +$9; **10–90s**
n=118 WR81% +$21.6 (TP60) / +$24.4 (TP70). EXTRA 46–90s strongly positive. Full-window
still dilutes. Default `SCALP_ENTRY_ELAPSED[5]` 45→**90** (cancel-left → 210s). Superseded same day → 120 + RSI off.

## 2026-11-07 — IMPLEMENTED: RSI good-mix (superseded → default off)

Buy **Up** only if RSI &lt; 40; buy **Down** only if RSI &gt; 60; unknown RSI skips.
On top of time + vel latch + Δ + TP (ATR still paused). Live-matched 60m: n=13 WR 85%
EV +$0.30 +$3.9 vs filters-only n=50 WR 58% +$5.6 (fewer trades, higher quality).
Would have blocked 2/4 of the latest losses; not the Nov 5 knife or Sat 00:00.
Was default on; paused 2026-11-08 after full-sample replay showed RSI cut +EV volume on 5m/15m/60m. Skip notes: `rsi_mix`, `rsi_unknown`.

## 2026-11-07 — IMPLEMENTED: pause ATR gate (default 0)

Live-matched 60m with time + vel≤$60 latch + Δ + TP70: **ATR off** n=54 WR 59% EV +$0.39
+$21 vs ATR≤$350 n=34 WR 59% EV +$0.38 +$13. Macro ATR was cutting volume, not adding
edge; ≤$250 was worse (WR 46%, −$). Defaults `SCALP_ATR_MAX_USD` → 0 all durations.
Re-enable with `--scalp-atr-max` (prior caps 60/125/350). Running-away detection stays
on velocity (+ latch) and Δ.

## 2026-11-08 — FIX: velocity latch without needing ≤30¢ ask

Bug: latch only armed when a side was already ≤30¢. UI vel (cheaper side) could show
$40+/min while asks were 40–50¢, then when the ask finally printed ≤30¢ vel had cooled
to ~$20 and the 5m window still entered. Live Nov-8 examples (11:20 / 11:30 / 11:50).
Fix: trip latch on adverse vel for the **cheaper ask side** even when ask > entry
(same side the UI shows). Cheap+cool later still blocked.

## 2026-11-07 — IMPLEMENTED: velocity window latch

Once adverse velocity exceeds the per-duration cap on the cheaper side (ask need not
be ≤30¢; fixed 2026-11-08), the window stays blocked for the rest of that window
(`velocity_latched`) — a stalled knife that later cools under $/min must not be
treated as quiet.

Live-matched 60m (in-gate fills): best cap remains **$60/min**; latch at $60
blocks the Nov 5 12:00 knife (−$2.12) and does not cut winners in that sample.
Skip notes: `velocity_high` (trip tick) then `velocity_latched` (later ticks).

## 2026-11-03 — IMPLEMENTED: 60m TP70 + ATR ≤ $350

Live-matched 60m (ATR≤$350, 61–300s, 30¢ touch): TP70 beat TP60 (same hit rate lately, +~$0.55 EV/trade). ATR ceiling raised $250→$350 so mid-vol reversals are not blocked.

## 2026-10-30 — IMPLEMENTED: scaled time / velocity / Δ (superseded)

Prior: 5m 120–180s / 15m 180–300s / 60m 60–300s; ATR flat $350. Replaced by 2026-10-31 tightened gates.

## 2026-10-29 — IMPLEMENTED: ATR + velocity + adverse Δ pipeline

Superseded timing/velocity/Δ scaling by 2026-10-30. ATR window shortened 24h→6h same day.

## 2026-10-29 — IMPLEMENTED: full formula all durations (30→TP60 + signed Δ)

Superseded by 2026-10-30 scaled windows. Prior: 5m 5–30s / 15m 15–120s / 60m 60–300s; Δ $20/$50/$200; velocity flat $60/min.

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
