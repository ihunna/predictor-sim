# PIL (plan in motion)

The only two strategies worth experimenting with, found on 12–13 Oct 2026 data (BTC 5m: ~255 windows,
15m: ~87). Trading stays as it is until the week of data is in. When the user says **"implement PIL"**,
build both strategies below.

Definitions used throughout:

- **Last move**: the previous window's official end price vs its start price, in percent.
- **Calm**: last move under 0.05% (5m). 15m: about 0.075–0.10%, recheck on the week's data.
- **Streak 2+**: the last two (or more) windows had the same winner. "No streak" = the last two winners differ.
- **Favourite**: the side with the higher ask at the window's open.

## Strategy 1: Dual, only in choppy windows

Trade the current dual (30¢ limit on Up and Down, 8 contracts each, cancel at 40% left with no fill,
live stop-loss at 20s with 3¢ minimum) **only when the last move is calm AND there is no streak**.
Skip every other window (log it as now).

Why: a streak means a trend, where one side runs away and fills are one-sided losses. Duals need a market
that swings both ways.

Evidence (30¢ × 8 replay, no lock/stop):

```
5m   calm + no streak     42 windows   54% duals   +$28.00   ← only profitable group
     calm + streak 2+     49           36%          −$8.80
     big  + no streak     93           32%         −$55.20
     big  + streak 2+     71           29%         −$52.80
     current filter (calm only): 91 windows +$19.20

15m  calm + no streak      8 windows   62% duals    +$8.80   (too few to trust)
```

## Strategy 2: Back the opening favourite (5m only)

At the window open, buy the favourite at its ask (taker, +2% fee) if it costs 55¢ or less, and hold to the
end. **Trade only after a streak of 2+** (strongest when the favourite goes against the streak). Also skip
"big last move + no streak". No hedge: hedging caps the winners and lost money in every test.

Evidence (favourite bought at open, held):

```
5m   all windows                        105/170  62%   +6.9¢/share   (break-even ~55%)
     calm + streak 2+                    35/49   71%  +16.2¢
     big  + streak 2+                    23/33   70%  +15.4¢
     calm + no streak                    24/40   60%   +4.7¢
     big  + no streak                    23/48   48%   −6.6¢   ← skip
     streak 2+, favourite AGAINST streak 38/49   78%  ~+22¢
15m  favourite: 54% at 53¢ → break-even, not used
```

Pending check: a trailing stop on the favourite (after it rises to 60–75¢, sell if it falls back to entry).
Needs the `*_rise_*` / `*_min_bid_after_*` columns logged from 13 Oct evening.

The two strategies complement each other: streak → favourite, calm and no streak → dual, big move and no streak → neither.

## Strategy 3 (candidate, added 14 Oct): Dual only in tight windows, entered early

Place the dual only in windows where BTC stays close to the price to beat, decided within the first
10–20 seconds. **Tight** = BTC ends within 0.02% of the price to beat (0.05% at most).

Why: how far BTC ends from the price to beat is the strongest pattern found. Windows that end far away
almost always end one-sided losing, because the only order that fills is on the side being left behind.

Evidence (every window, 30¢ × 8 replay from each side's lowest price, no stop-loss, 12–14 Oct):

```
5m   this window's end move   windows   duals   one-sided lost   P&L
     under 0.02%                110      69%        29%         +$177.60
     0.02–0.05%                  99      39%        61%          −$19.20
     0.05–0.10%                 143      31%        69%          −$96.80
     over 0.10%                 135      19%        81%         −$184.00
15m  under 0.05%                 41      51%        49%          +$19.20
     0.05% or more              126      33%        67%          −$67.20
```

Timing matters: if a 5m window will end within 0.02% and we only become sure of it at time T, entering at T
still gets both fills in:

```
     10s in   64%   +$99        60s in   41%   +$56
     20s in   62%   +$96        90s in   29%   +$37
     30s in   54%   +$67        2 min    22%   +$34      3 min   7%   −$5
     (open: 67%, +$100; 69 windows)
```

So the decision has to come within about 20 seconds of the open, or most duals are already gone.

What does NOT pick out tight windows (tested 14 Oct): the last window's move, the average of the last 3–4
windows, "last 3 all calm", and time of day. They help avoid the worst windows (after big moves), nothing more.
Waiting with an order-book check (no side ≤40¢ yet) also failed: the price is too jumpy to stand in for BTC.

To test after the week (needs the logging added 14 Oct: `up/down_ask_{10..120}s_in`,
`btc_move_pct_{10..120}s_in`, `btc_up_0.05pct_time_left`, `btc_down_0.05pct_time_left`):

1. **Early check**: if BTC is within X% (try 0.01–0.03%) at 10s / 20s in, how often does the window end
   tight, and does placing the dual then make money?
2. **Breakout cancel**: place at the open, cancel any unfilled order the moment BTC first gets 0.05% away.
   Compare saved one-sided losses against duals lost (windows that cross 0.05% and come back).
3. **Combined**: early check + breakout cancel + the existing stop-loss.

## When "implement PIL" is said

1. **Recheck first** on the week's logs: rerun both tables. The best groups are only 22–49 windows, and
   many splits were tried on the same data, so some of the edge may be luck. Report what held up and
   adjust the thresholds (calm cut-off, streak length, 55¢ cap) before building.
2. **Dual**: add a streak filter next to `--max-prev-move-pct`, e.g. `--skip-after-streak 2` (default on),
   with skip reason `streak_<n>` logged like `prev_move_*`.
3. **Favourite**: a second simulator in the same bot process (shares the socket, prices and window loop),
   its own trades log `{coin}-{duration}-predict-fav-trades.csv` and its own capital, shown as a separate
   panel in the dashboard. Options: `--fav` on/off, `--fav-max-price 55`, `--fav-contracts`, streak/move rules.
4. Tests for both, run in log-only mode first if the recheck is borderline.
