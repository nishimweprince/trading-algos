# Pre-registration: Phase 1 shadow → Phase 2 live gate

**Commit this file before the shadow collection starts. Don't edit it afterwards.**
If any parameter below changes, the collected data no longer counts toward
the gate and a new pre-registration and collection window begin.

## Hypothesis

From an AWS Tokyo host, taker/taker IOC arbitrage between Binance USDⓈ-M and
Bybit linear USDT perpetuals captures enough executable basis to clear real
account fees, measured latency, depth impact, legging and funding, at a mean
of at least 8 bp per completed cycle.

## Frozen configuration

- Build: the commit hash recorded in `docs/aws-tokyo.md` at deployment.
- Basket: the `CXA_BASKET` value in `/etc/crossex.env`, recorded below at
  deployment and not changed during the window.
- `CXA_LATENCY_MODE=pessimistic` (p95 of measured signed RTT/2, plus 1 ms).
- `CXA_TRADE_NOTIONAL=1000`, `CXA_AVAILABLE_PPM=500000`.
- All other strategy parameters at the defaults in
  [`STRATEGY_SPEC.md`](STRATEGY_SPEC.md).
- Read-only keys, so fees are the account's real taker rates.

```
basket:      <fill in at deployment>
commit:      <fill in at deployment>
start (UTC): <fill in at deployment>
```

## Gate

`crossex daily-report /data/crossex/*.jsonl*` computes every criterion. All of
them must be true:

| Criterion | Threshold |
|---|---|
| Latency mode on every file | `pessimistic` |
| Coverage (sum of run spans) | ≥ 14 days |
| Completed cycles (excluding `missed`) | ≥ 150 |
| Mean net per cycle | ≥ 800 ppm (8 bp) of entry notional |
| Bootstrap 95% CI lower bound of the mean (10,000 resamples, fixed seed) | > 0 |
| Legging rate (cycles with any repair, or `legged_out`) | ≤ 10% |
| Worst single-cycle loss | ≤ 20 × mean net cycle P&L (USD) |

## Decision rule

- **Pass:** proceed to M5 (live taker/taker IOC gateways), then the M6 canary
  at $100–250 per pair. Each later size increase must pass the same table,
  measured on live fills.
- **Fail:** stop. Write up `census-report` (frequency and persistence of
  executable gaps) and `daily-report` as the result. Do not lower thresholds,
  narrow the basket to the symbols that happened to win, or change the
  entry/exit parameters, then re-score the same data.

## Secondary (reported, not gating)

- Census: episodes per day at ≥30/40/50/75 bp for each size, and the share
  lasting ≥500 ms.
- Realised versus expected net per cycle (`mean_expected_net_ppm`), which
  measures how optimistic the decision-time model is.
- The same journals replayed with `CXA_LATENCY_MODE=sampled`, to show how
  sensitive the result is to latency.
