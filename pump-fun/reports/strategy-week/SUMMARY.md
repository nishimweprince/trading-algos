# Strategy Week Review

Generated: 2026-09-18T11:10:01.683Z  
Range: **7d** · Mode filter: **live** · Schema v1

## Executive summary

- Trades (n): **18** · Win rate: **22.2%** · Expectancy: **-0.00474 SOL**
- Profit factor: **0.14** · Max DD: **0.0853 SOL** · Fees: **0.0105 SOL**
- Left-on-table (avg MFE − realized %): **21.8%**
- Execution: detect→open p~avg **1310 ms**, exit confirm avg **1257 ms**, emergencies **0**, failed **15**

## Config sessions

- session **30** hash=`a8fe4144f25efd22` mode=live start=2026-09-18 10:54:25 git=1a7a61b
- session **29** hash=`a8fe4144f25efd22` mode=live start=2026-09-18 10:50:19 git=82e2aad
- session **28** hash=`a8fe4144f25efd22` mode=live start=2026-09-18 10:19:13 git=15e43eb
- session **27** hash=`a8fe4144f25efd22` mode=live start=2026-09-18 10:14:19 git=422ced0
- session **26** hash=`a8fe4144f25efd22` mode=live start=2026-09-18 10:03:20 git=83e2e55
- session **25** hash=`a8fe4144f25efd22` mode=live start=2026-09-18 09:35:57 git=b72f11d
- session **24** hash=`a8fe4144f25efd22` mode=live start=2026-09-18 09:09:02 git=683ccd9
- session **23** hash=`a8fe4144f25efd22` mode=live start=2026-09-18 09:05:41 git=683ccd9
- session **22** hash=`a8fe4144f25efd22` mode=live start=2026-09-18 08:46:42 git=0694a0c
- session **21** hash=`4dce1539b23b1ac1` mode=live start=2026-09-17 20:41:56 git=57d9196
- session **20** hash=`fb066763f417265c` mode=live start=2026-09-17 20:30:01 git=107efb3
- session **19** hash=`fb066763f417265c` mode=live start=2026-09-17 20:20:23 git=9e2b1c2
- session **18** hash=`fb066763f417265c` mode=live start=2026-09-17 19:41:05 git=6fafa0a
- session **17** hash=`fb066763f417265c` mode=live start=2026-09-17 18:49:08 git=6fafa0a
- session **16** hash=`fb066763f417265c` mode=live start=2026-09-17 09:17:51 git=398f52d
- session **15** hash=`fb066763f417265c` mode=live start=2026-09-17 09:11:56 git=398f52d
- session **14** hash=`fb066763f417265c` mode=live start=2026-09-17 09:00:09 git=398f52d
- session **13** hash=`fb066763f417265c` mode=live start=2026-09-17 08:47:57 git=398f52d
- session **12** hash=`fb066763f417265c` mode=live start=2026-09-17 08:02:16 git=398f52d
- session **11** hash=`e5e55f11f8481044` mode=live start=2026-09-17 07:58:13 git=e3153d9

## Strata: exit reason

| Reason | n | WR% | Exp SOL | PF | left-on-table% | underpowered |
| --- | --- | --- | --- | --- | --- | --- |
| STOP_LOSS | 13 | 0 | -0.0077 | 0.00 | 25.5 | false |
| TRAILING_STOP | 5 | 80 | 0.0029 | 255.84 | 12.0 | false |

## Strata: soft-score bucket

| Bucket | n | WR% | Exp SOL | PF | underpowered |
| --- | --- | --- | --- | --- | --- |
| 80-90 | 10 | 10 | -0.0066 | 0.05 | false |
| 90+ | 3 | 67 | 0.0008 | 1.32 | true |
| 70-80 | 3 | 33 | -0.0013 | 0.18 | true |
| 60-70 | 2 | 0 | -0.0091 | 0.00 | true |

## Strata: momentum window (ms)

| Window | n | Exp SOL | PF | underpowered |
| --- | --- | --- | --- | --- |
| 0 | 12 | -0.0069 | 0.01 | false |
| 250 | 5 | -0.0008 | 0.75 | false |
| 750 | 1 | 0.0020 | 999.00 | true |

## Selection funnel

- Graduations: **2004** · Accepted: **34** · Vetoed: **1969** · Accept rate: **1.70%**
- Entered: **33** · Closed: **18** · Failed: **15**

### Why candidates were vetoed (primary cause)

| Reason | Category | n | % of vetoed |
| --- | --- | --- | --- |
| UNKNOWN:H4 | unknown | 1191 | 60.5 |
| UNKNOWN:H1 | unknown | 304 | 15.4 |
| H5 | hard_fail | 156 | 7.9 |
| LOW_SCORE | low_score | 155 | 7.9 |
| H1 | hard_fail | 57 | 2.9 |
| UNKNOWN:H3 | unknown | 45 | 2.3 |
| H4 | hard_fail | 30 | 1.5 |
| H7 | hard_fail | 14 | 0.7 |
| UNKNOWN:H5 | unknown | 6 | 0.3 |
| H3 | hard_fail | 6 | 0.3 |
| H10 | hard_fail | 5 | 0.3 |

_`unknown` = enrichment reliability (recoverable by budget/retry, NOT by relaxing safety); `hard_fail` = structural risk rejection; `low_score` = soft-score gate._

### Veto dry-run vs live accepted (counterfactual performance)

_Capital-free dry-runs of vetoed coins through the same exit policy; not mixed with live PnL._

Live accepted: **n=18** · win **22.2%** · expectancy **-0.00474 SOL** · net **-0.0853 SOL**
Veto dry-runs: **83 tracked / 83 exit sims / 0 legacy peaks**

| Veto reason | n | Win% | Expectancy | Net PnL | Avg MFE% | Hit50% |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| H5 | 50 | 76.0 | 0.13918 | 6.9592 | 479.6 | 60.0 |
| UNKNOWN:H1 | 24 | 70.8 | -0.00304 | -0.0730 | 12.7 | 0.0 |
| H10 | 5 | 80.0 | 0.00405 | 0.0202 | 21.0 | 0.0 |
| UNKNOWN:H5 | 2 | 50.0 | -0.00849 | -0.0170 | 18.1 | 0.0 |
| UNKNOWN:H4 | 1 | 100.0 | 0.07314 | 0.0731 | 245.6 | 100.0 |
| H7 | 1 | 100.0 | 0.02989 | 0.0299 | 101.5 | 100.0 |

## Hypotheses (rule-based)

### H-stop-loss-drag
- **Claim:** Hard stop exits are a material expectancy drag.
- **Evidence:** `{"n":13,"expectancySol":-0.0076689184265008314,"avgMaePct":-22.473623875004044}`
- **Touchpoints:** src/exits/engine.ts, config.yaml#exits.hardStopPct
- **Suggested change:** Check if hardStopPct is too tight for live volatility; compare MAE distribution.

### H-exit-quality
- **Claim:** Average left-on-table is high — exit ladder may cut winners early.
- **Evidence:** `{"n":18,"avgLeftOnTablePct":21.7754589118072,"globalExpectancy":-0.0047387066447976585}`
- **Touchpoints:** src/exits/engine.ts
- **Suggested change:** Review tp1SellFraction / tp2Pct / trailing; inspect path_marks on best vs worst trades.

## Code map

- **scoring:** `src/guardrails/scoring.ts`
- **exits:** `src/exits/engine.ts`
- **entry:** `config.yaml#entry`
- **guardrails:** `config.yaml#guardrails`
- **risk:** `src/risk/manager.ts`
- **positions:** `src/positions/manager.ts`
- **fees:** `config.yaml#fees`

## Worst trades (for qualitative review)

- `91ryaCo5yGpYZM3bs6GUPs97VWJQj7RozBmqPULgpump` net=-0.0160 exit=STOP_LOSS score=84 MFE=0.0% left=37.2%
- `AJQ48erLGxjwqFAZR1vm9HBU518ArDY9iU6sceaVpump` net=-0.0109 exit=STOP_LOSS score=62 MFE=0.0% left=25.8%
- `CWaGFU2xboUfSx3TN7WytRcHKvccFjvqPPK9LWWwpump` net=-0.0096 exit=STOP_LOSS score=85 MFE=0.0% left=21.9%
- `4GP6tgNysAtVgpicD9DXLXCvfStW8Ck85ErjTtqopump` net=-0.0085 exit=STOP_LOSS score=85 MFE=0.0% left=41.8%
- `Y7F6siYVVfieGTnyFWpUE8nw8TujkvuoCkF45PNpump` net=-0.0082 exit=STOP_LOSS score=85 MFE=6.1% left=26.2%

## Caveats

- Mode filter: live. Live pilot n is often small — treat strata with underpowered=true as directional only.
- Closed trades in range: 18. Prefer config knobs over structural rewrites when n < 30.
- Fees on paper are estimated; live fees depend on recorded fees_sol.
- Path marks / MFE timing only exist for trades collected after path instrumentation shipped.
- Do not mix conclusions across config_hash sessions without checking configSessions.

## Instructions for coding model

1. Prefer **config.yaml** knob changes over large rewrites when n is small.
2. Only act on hypotheses with `underpowered: false` unless explicitly exploring.
3. Separate **execution** issues (latency/fees/fails) from **exit/selection** edge.
4. Propose a minimal PR plan with files from codeMap; re-run this report after the next pilot week.
