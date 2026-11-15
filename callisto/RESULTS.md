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

## Defaults changed (2026-11-15)
`SL_PIPS=80`, `TP_PIPS=150` and `TRAIL_START_PIPS=0` (no trailing) are now the defaults in
`config.py` and `.env.example`, chosen from the SL sweep above. The tables earlier in this
file were produced with the old defaults (SL 60, trail 50/30/10). Trailing is still available
by setting `TRAIL_START_PIPS`.

---

# One-month backtest: 2026-10-15 → 2026-11-15 (run 2026-11-15)

## Pipeline counts
- 3,210 posts classified: 1,231 signal, 757 promo, 400 member_result, 394 closed_result,
  225 other, 203 analysis.
- **125 ideas**, of which 40 are reposts, 18 stale and 3 posted while the market was closed,
  leaving **64 traded**.
- Age at first sight: median 48 pips in profit, about 7 minutes after price was last at the
  stated entry.

## Current defaults (SL 80 / TP 150 / no trailing)
| model | trades | win % | net pips | t | $/lot |
|---|---|---|---|---|---|
| follower | 64 | 42% | +1,090 | 1.19 | +$10,900 |
| channel (stated entry, optimistic) | 64 | 61% | +3,928 | 4.43 | — |

By week (follower, week starting Monday):
| week | trades | net pips | BUY | SELL |
|---|---|---|---|---|
| Oct 12 | 4 | −90 | +70 | −160 |
| Oct 19 | 13 | −120 | −100 | −20 |
| Oct 26 | 13 | +110 | −250 | +360 |
| Nov 2 | 17 | +250 | +350 | −100 |
| Nov 9 | 17 | **+940** | −260 | +1,200 |

**Out-of-sample check.** SL 80 was chosen on the Nov 9 week. On the 47 trades before it
(Oct 15 – Nov 8) the defaults made **+150 pips, t 0.2**: no edge. In that window SL 80 is
also an isolated spike, with SL 70 at −210 and SL 90 at −150, so the earlier "plateau" did
not hold. Without trailing, 11 of the 13 out-of-sample SL cells are negative.

## Sweeps (`data/results/sweeps_31d.txt`)
Net pips, TP 150:
| SL | month, trail 50/30/10 | month, no trail | Oct 15–Nov 8, trail | Oct 15–Nov 8, no trail |
|---|---|---|---|---|
| 30 | +156 | −120 | +74 | −330 |
| 50 | +240 | −200 | −5 | −550 |
| 60 | +196 | −60 | −136 | −510 |
| 70 | +404 | +360 | +132 | −210 |
| 80 | +471 | **+1,090** | +135 | +150 |
| 90 | +483 | +720 | +197 | −150 |
| 100 | +388 | +600 | +27 | −200 |
| 110 | +475 | +500 | +153 | −230 |
| 120 | **+822** | +420 | **+541** | −240 |
| 130 | +819 | +360 | +577 | −230 |
| 140 | +669 | +30 | +467 | −490 |
| 150 | +740 | −300 | +578 | −750 |

- **No trailing** only works on the full month because the Nov 9 week is in it. Before that
  week it loses almost everywhere.
- **Wide stop with trailing (SL 120–150, TP 150, trail 50/30/10)** is the only region that is
  positive and flat in both windows: +669 to +822 on the month and +467 to +578 out of
  sample. Win rate is 75–79%, PF about 1.4, but t is still about 1.0–1.2. The trail closes
  most trades (4 of 64 reach TP), so TP barely matters there.
- With SL 80 at the TP end, TP 180–220 without trailing looks best on the month
  (+1,380 / +1,780). Out of sample it is +400 / +440 at t < 0.5, and TP 240+ turns negative,
  so I wouldn't use it.
- No side is consistently right: sells carry the full month at the current defaults
  (+1,280 vs buys −190), while out of sample the wide-stop trail is carried by buys
  (+586 to +833).

## Verdict (one month)
Following the channel is roughly break-even to slightly positive, and not significant: the
best robust setting gives about +12 pips per trade at t ≈ 1.2 on 64 trades. The channel's
own entries look strong (+3,928, t 4.4), but a follower arrives about 48 pips late and keeps
roughly a quarter of that. The SL 80 / no-trail defaults were fitted to one week and failed
on the three weeks before it. Detection speed (seconds, not minutes) and entering near the
stated entry would matter more than any SL/TP choice.

## Defaults changed again (2026-11-15, after the month run)
The defaults are now `SL_PIPS=120`, `TP_PIPS=150`, `TRAIL_START_PIPS=50`,
`TRAIL_DISTANCE_PIPS=30` and `TRAIL_STEP_PIPS=10`. These replace SL 80 / no trailing, which
failed the out-of-sample check above.

Month with the new defaults: 64 trades, 75% win rate, **+822 pips**, t 1.23, worst trade −120.
By week (net / BUY / SELL):
| week | trades | net | BUY | SELL |
|---|---|---|---|---|
| Oct 12 | 4 | −166 | −69 | −96 |
| Oct 19 | 13 | +319 | +394 | −75 |
| Oct 26 | 13 | +350 | +153 | +197 |
| Nov 2 | 17 | +37 | +108 | −71 |
| Nov 9 | 17 | +282 | −286 | +567 |

4 of 5 weeks are positive and the profit is spread across weeks rather than coming from a
single one, unlike SL 80 / no trail, which had +940 in one week and +150 in the rest.
It is still not significant.
