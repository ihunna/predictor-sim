# To-be-added (re-check before shipping)

Candidates that looked promising on a snapshot but are **not** defaults yet.
Re-run the noted replay after more windows accumulate; only promote if the edge holds
live-matched (same gates/fills as the bot).

---

## Against: `struct_rsi` + UTC hours **12:00–20:00** only

**Status:** pending re-check · not implemented  
**Proposed:** after exact-2 + `struct_rsi` pass, skip unless window start hour ∈ [12, 20) UTC.  
**Do not use 08–20** — that band includes weak **08–12** and washes out the lift.

### Snapshot evidence (2026-11-12, exact-2 break WR / outcome)

| Slice (15+60, `struct_rsi` pass) | n | break WR |
|----------------------------------|---|----------|
| All hours | 147 | 55.8% |
| **12–20 UTC** | **43** | **62.8%** |
| 08–20 UTC (one block) | 69 | 55.1% (no lift) |
| 08–12 UTC | 26 | 42.3% |

Weekday vs weekend was flat (~55.7% / 56.1%) — not a candidate.

Live ≤50¢ hold fills for hour buckets were too thin to trust at snapshot time.

### Recheck

1. Rebuild exact-2 + `struct_rsi` break WR for **12–20 vs other** on 15m/60m (and 5m) updown.
2. Live-matched AgainstSim: `struct_rsi`, N=2, ≤50¢, hold; compare all-hours vs hour gate 12–20 (n, W, L, WR, EV, Σ).
3. Promote only if 12–20 still beats all-hours on WR **and** live EV/Σ with usable n; else drop or revise.
