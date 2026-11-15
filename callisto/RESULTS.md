# First backtest: 2026-11-08 → 2026-11-15 (run 2026-11-15)

## Pipeline counts
- 780 posts in the window, all classified by `gpt-5.4-mini`: 299 signal, 212 promo,
  107 member_result, 62 other, 60 closed_result, 40 analysis.
- 299 signal posts fold into **33 ideas**. Skipped: 12 reposts (recycled screenshots; the
  4040.16 buy alone is reposted on Nov 10, 11, 13 and 15), 3 stale (entry 270–920 pips from
  market), 1 market closed. **17 traded.**
- Age at first sight (traded ideas): median **48 pips** already in profit, median **9 min**
  since price was last at the stated entry.

## Result, default rules (SL 60, TP 150, trail 50/30/10, 24 h max, 30 s delay)
| model | trades | win % | net pips | avg | t | $/lot |
|---|---|---|---|---|---|---|
| follower (enter at first post) | 17 | 65% | **+332** | +19.5 | 1.12 | +$3,322 |
| channel (stated entry, est. call time, optimistic) | 17 | 94% | +1,181 | +73.8 | — | — |

By side (follower): **SELL 8 trades +567 pips, BUY 9 trades −235 pips.** Gold fell about $250
over the week (≈4140 → 3880). The profit comes from shorting into that move, not from the
signals being right in both directions.

## Sensitivity (follower)
| change | net pips | t |
|---|---|---|
| entry delay 10 s / 120 s / 300 s / 900 s | +340 / +283 / +572 / **+5** | 1.15 / 0.91 / 2.06 / 0.02 |
| no trailing | +450 | 1.02 |
| trailing starts at 100 | +388 | 1.00 |
| stale filter off (20 trades) | +324 | 1.06 |
| grid of 100 SL/TP/trail cells | 73/100 positive; best SL 80 / TP 400 no trail +2,703 | in-sample |

Grid medians: no trailing +610, trail 30/20/10 −36. Wider TP is better (TP 50 → +50,
TP 250/400 → +251). That pattern is what a one-directional week looks like. It is not a
setting to lock in.

## Verdict
Not proven. 17 trades over one trending week gives t ≈ 1. The gap between the channel's book
(+1,181) and a follower's (+332) is the cost of entering ~50 pips late. That cost grows with
detection delay: entering 15 minutes late leaves nothing. Keep collecting live with
`monitor.py` and re-run `backtest.py` weekly. Settle SL/TP/trailing only once there are
several weeks of trades and a week in which the market goes up.

## TP sweep at SL 60 (`sweep_tp.py`, 17 trades, follower, 30 s delay)
| TP | trail 50/30/10: net | win % | PF | no trail: net | win % | PF |
|---|---|---|---|---|---|---|
| 60 | +192 | 65% | 1.53 | +180 | 59% | 1.43 |
| 70 | +229 | 65% | 1.64 | +280 | 59% | 1.67 |
| 80 | +222 | 65% | 1.62 | **+380** | 59% | **1.90** |
| 90 | +224 | 65% | 1.62 | +180 | 47% | 1.33 |
| 100 | +264 | 65% | 1.73 | +260 | 47% | 1.48 |
| 110 | +265 | 65% | 1.74 | +340 | 47% | 1.63 |
| 120 | +295 | 65% | 1.82 | +240 | 41% | 1.40 |
| 130 | +292 | 65% | 1.81 | +310 | 41% | 1.52 |
| 140 | +312 | 65% | 1.87 | +380 | 41% | 1.63 |
| 150 | **+332** | 65% | **1.92** | **+450** | 41% | 1.75 |

- Every cell is profitable, and none is significant (t 0.6–1.3).
- **With trailing**, net rises almost steadily with TP. There is no peak inside 60–150; the best is the top of the range. In the earlier grid it keeps rising up to TP 400. Win rate stays at 65% because the trail exits the trades that don't reach TP.
- **Without trailing**, the curve zig-zags. TP 80 (+380, PF 1.90) is a local peak, but its neighbours are +280 and +180: going from 80 to 90 turns two winners into stop-outs. That makes it a single-trade artefact, not a sweet spot.
- In every cell, buys lose and sells win. A larger TP pays only because the week trended down.
- Choice: keep **TP 150 with trailing**. It has the smoothest curve, the best PF, the same 65% win rate, and it doesn't depend on one trade flipping. Re-run once a few more weeks are in, ideally including a week when gold rises.
