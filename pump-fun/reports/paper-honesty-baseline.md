# Paper-honesty baseline (pre-change regression anchor)

- Source: Frankfurt `full-bundle` export at 2026-10-05T17:11:07.810Z (mode=dry-run, range=7d).
- Code: `feat/migration-racer` @ `3ffc0e7` (racer + dashboard as deployed).
- Migrations tracked: 300/300; coverage: {"legacy-unverified": 50, "missing-anchor": 63, "verified": 109, "unpriced:no-virtual-quote": 2, "unpriced:non-wsol-quote": 29, "unpriced:thin-pool": 47}.
- Paper tracks: 222 (verified 109, excluded {"missing-anchor": 63, "legacy-unverified": 50}).
- Costs: measured share 0.649, median live cost 0.0005438 SOL, ATA rent est 0.00203928 SOL.
- Sizing (verified): {"reasons": {"ok": 35, "legacy-fixed": 74}, "medianWalletBalanceSol": 0.214272396, "medianSizeSol": 0.1}.
- Slots fire→complete p50/p90: 6/87.6 (n=113); complete→migrate p50/p90: 2/4.
- Competition: 476 attempts, 176 failed (0.587/migration), contested share 0.370.
- On-chain slot-1 fill rate: 0.6394 (513 attempts, 185 failed); mean failed fee 1.05e-05 SOL.

## Verified-cut expectancy by leg × horizon (percent of size, net of liveCostSol)

| horizon | leg | n | median % | win % |
|---|---|---|---|---|
| 30s | open | 107 | 1.16 | 52.3 |
| 30s | reach | 107 | 1.16 | 52.3 |
| 30s | slot1 | 107 | 0.14 | 50.5 |
| 30s | slot1mid | 107 | -1.17 | 46.7 |
| 30s | slot2 | 107 | -1.17 | 47.7 |
| 30s | slot2mid | 107 | -1.42 | 46.7 |
| 30s | slot3 | 107 | -1.24 | 46.7 |
| 30s | slot3mid | 107 | -2.36 | 46.7 |
| 30s | feasible | 51 | -2.84 | 47.1 |
| 30s | feasiblemid | 51 | -2.84 | 43.1 |
| 60s | open | 106 | 1.86 | 52.8 |
| 60s | reach | 106 | 1.86 | 52.8 |
| 60s | slot1 | 106 | -1.96 | 46.2 |
| 60s | slot1mid | 106 | -4.23 | 45.3 |
| 60s | slot2 | 106 | -4.56 | 44.3 |
| 60s | slot2mid | 106 | -4.56 | 46.2 |
| 60s | slot3 | 106 | -4.58 | 44.3 |
| 60s | slot3mid | 106 | -3.93 | 43.4 |
| 60s | feasible | 50 | -5.22 | 40.0 |
| 60s | feasiblemid | 50 | -6.42 | 42.0 |
| 300s | open | 86 | -17.84 | 40.7 |
| 300s | reach | 86 | -17.84 | 40.7 |
| 300s | slot1 | 86 | -22.09 | 40.7 |
| 300s | slot1mid | 86 | -22.24 | 39.5 |
| 300s | slot2 | 86 | -22.34 | 39.5 |
| 300s | slot2mid | 86 | -22.47 | 38.4 |
| 300s | slot3 | 86 | -22.54 | 38.4 |
| 300s | slot3mid | 86 | -21.69 | 37.2 |
| 300s | feasible | 45 | -23.09 | 40.0 |
| 300s | feasiblemid | 45 | -23.32 | 37.8 |

## pathExit rules (same-trade-time baseline in source export)

| rule | n | unpriced | notCaptured | mean % | median % | win % |
|---|---|---|---|---|---|---|
| stop20 | 30 | 29 | 27 | -6.59 | -20.27 | 20.0 |
| take30 | 30 | 29 | 27 | -8.24 | 14.22 | 56.7 |
| trail15 | 30 | 29 | 27 | 6.05 | -0.94 | 46.7 |
| combined | 30 | 29 | 27 | -0.24 | -5.95 | 40.0 |
| time300 | 30 | 29 | 27 | -16.15 | -36.01 | 33.3 |

Evaluation: {"verifiedCompleted": 86, "exploratoryRows": 86, "reservedLaterRows": 0, "selectionReady": false, "validated": false}.

## How to compare after each unit

Re-run paper over the stored migration set (or a fresh soak window) and diff this table.
Every unit that moves a verified-cut cell must explain why in its commit message.
New honesty adjustments land as ADJACENT legs/columns, never silent rewrites, so before/after stays readable.
