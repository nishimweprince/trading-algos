# Pre-registration: Phase 1 shadow → Phase 2 live gate

**Commit this file before the shadow collection starts. Don't edit it afterwards.**
If any parameter below changes, the collected data no longer counts toward
the gate and a new pre-registration and collection window begin.

## Hypothesis

From an AWS Tokyo host, maker/taker (PostOnly join + taker hedge) arbitrage
between Binance USDⓈ-M and Bybit linear USDT perpetuals, at $250 matched
notional under pessimistic measured latency, produces a large enough shadow
trade sample to estimate mean net edge after real account fees, depth impact,
legging and funding.

## Frozen configuration

- Build: the commit hash recorded in `docs/aws-tokyo.md` at deployment.
- Basket: the `CXA_BASKET` value in `/etc/crossex.env`, recorded below at
  deployment and not changed during the window.
- `CXA_EXEC_MODE=maker_taker`, `CXA_MAKER_TIMEOUT_MS=2000`.
- `CXA_LATENCY_MODE=pessimistic` (p95 of measured signed RTT/2, plus 1 ms).
- `CXA_TRADE_NOTIONAL=250`, `CXA_AVAILABLE_PPM=500000`, `CXA_MIN_NET_PPM=0`.
- All other strategy parameters at the defaults in
  [`STRATEGY_SPEC.md`](STRATEGY_SPEC.md).
- Account maker and taker rates from the APIs (read-only keys preferred).

```
basket:      BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,1000PEPEUSDT,SUIUSDT,WIFUSDT,AVAXUSDT,LINKUSDT,ADAUSDT,LTCUSDT,ARBUSDT,OPUSDT,NEARUSDT,APTUSDT,INJUSDT,TIAUSDT,SEIUSDT,FILUSDT,ATOMUSDT,ORDIUSDT,1000BONKUSDT,WLDUSDT,JUPUSDT,PENDLEUSDT,ENAUSDT,BCHUSDT,DOTUSDT,UNIUSDT
commit:      working-tree maker_taker (host binary sha256 7286f7f5e55774dc970ddfdd17de5aaf22af1c15)
start (UTC): 2026-10-20T22:48:01Z
```

## Gate

`crossex daily-report /data/crossex/*.jsonl*` computes every criterion. All of
them must be true:

| Criterion | Threshold |
|---|---|
| Latency mode on every file | `pessimistic` |
| Coverage (sum of run spans) | ≥ 14 days |
| Completed cycles (excluding `missed`) | ≥ 150 |
| Legging rate (cycles with any repair, or `legged_out`) | ≤ 10% |

## Decision rule

- **Pass:** the sample is large enough to evaluate. Proceed to Phase 2 only if
  the reported mean net edge (and CI) justify live canary risk; then M5
  taker/taker or maker/taker IOC gateways and the M6 canary at $100–250 per
  pair.
- **Fail:** stop. Write up `census-report` and `daily-report` as the result.
  Do not lower thresholds, narrow the basket to the symbols that happened to
  win, or change the entry/exit parameters, then re-score the same data.

## Reported (not gating) — evaluate edge from the trade list

- Mean net ppm / USD per completed cycle
- Bootstrap 95% CI lower bound of the mean (10,000 resamples, fixed seed)
- Worst single-cycle loss (USD)
- Realised versus expected net (`mean_expected_net_ppm`)
- Census: episodes per day at ≥30/40/50/75 bp for each size, and the share
  lasting ≥500 ms
- The same journals replayed with `CXA_LATENCY_MODE=sampled`, to show latency
  sensitivity
