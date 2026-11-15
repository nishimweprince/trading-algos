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

## TP sweep at SL 60 (`sweep.py --param tp --sl 60`, 17 trades, follower, 30 s delay)
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

## SL sweep at TP 150 (`sweep.py --param sl --tp 150 --from 30 --to 100`)
| SL | trail 50/30/10: net | win % | PF | t | no trail: net | win % | PF | t |
|---|---|---|---|---|---|---|---|---|
| 30 | +81 | 29% | 1.23 | 0.31 | +210 | 24% | 1.54 | 0.65 |
| 40 | +239 | 47% | 1.66 | 0.85 | +460 | 35% | 2.05 | 1.19 |
| 50 | +244 | 53% | 1.61 | 0.82 | +350 | 35% | 1.64 | 0.86 |
| 60 | +332 | 65% | 1.92 | 1.12 | +450 | 41% | 1.75 | 1.02 |
| 70 | +272 | 65% | 1.65 | 0.87 | +570 | 47% | 1.90 | 1.22 |
| 80 | +337 | 71% | 1.84 | 1.06 | **+940** | 59% | **2.68** | **1.95** |
| 90 | +287 | 71% | 1.64 | 0.86 | +870 | 59% | 2.38 | 1.73 |
| 100 | +362 | 76% | 1.90 | 1.10 | +800 | 59% | 2.14 | 1.53 |

- **With trailing**, results are flat (+240 to +360) from SL 40 to SL 100. SL 30 is too tight: 12 of 17 trades stop out. From SL 60 up, sells are a constant +567, so only the buys change the total.
- **Without trailing**, there is a real plateau at **SL 80–100** (+800 to +940), with SL 80 on top: +940 pips, 59% win rate, PF 2.68, t 1.95. Unlike the TP 80 bump, its neighbours hold up, so this is the sweet spot of this grid.
- The reason it works: at SL 80 all 8 sells survive their early pullbacks and reach TP 150 (8 × 150 = +1,200). Buys still lose (−260). Trailing caps those sells at +567, which is why no-trail wins at wider stops. This is the same falling-week effect as before.
- In this sweep, adding a trail lowers net pips in every cell. On one trending week, keeping the trail is a bet that next week won't trend as cleanly.
- Caveat: SL 80 / TP 150 was chosen from about 60 tested cells on 17 trades from one week, so t 1.95 overstates the evidence.
