# Pre-registration: Binance ↔ Hyperliquid shadow → live gate

**Fill in the block below at deployment and commit it before the shadow
collection starts. Don't edit it afterwards.** If any parameter below
changes, the collected data no longer counts toward the gate, and a new
pre-registration and collection window begin.

This gate is separate from [`PREREG.md`](PREREG.md) (Binance ↔ Bybit). The two
instances run side by side on the same host and are scored independently.
Neither result can be used to tune the other.

## Prior window (does not count)

The first HL shadow window (started 2026-10-21, maker open + maker close,
2 s timeout) failed the gate early: ~0.35 coverage days, legging ~83%, mean
net about −650 ppm versus expected about +500 ppm. Those journals must not be
scored for this gate. Archive them aside before starting this window.

## Hypothesis

From an AWS Tokyo host, **maker entry with taker close** (PostOnly join plus
taker hedge on open; both-venue IOC flatten on exit) between Binance USDⓈ-M
perpetuals and Hyperliquid perps, at $250 matched notional, produces a large
enough shadow trade sample to estimate mean net edge. The sample is measured
under:

- pessimistic measured latency plus Hyperliquid block inclusion;
- real account fees (including the higher round-trip cost of taker close);
- top-of-book depth on Hyperliquid;
- longer open maker timeout, cancel cushion, and one requote;
- legging, and hourly versus 8-hour/4-hour funding.

## How Hyperliquid is modelled (frozen with the build)

- **Book.** The book is Hyperliquid's `bbo` only, so L1 is the whole book.
  The public `l2Book` feed pushed only about every 5.4 s (measured
  2026-10-21) and is not used. Any sweep that needs more than L1 size is
  treated as unfillable.
- **Leg latency.** A Hyperliquid leg arrives after
  p95(`exchangeStatus` RTT)/2 + 1 ms + `CXA_HL_INCLUSION_NS`. A Binance leg
  arrives after p95(signed RTT)/2 + 1 ms.
- **Funding.** Funding is `activeAssetCtx.funding`, paid hourly at the top
  of the hour.
- **Margin currency.** Prices are USDC on Hyperliquid and USDT on Binance.
  The USDT/USDC basis is absorbed by the 30-minute EWMA basis mean, as with
  any static basis.

## Frozen configuration

- Build: the commit hash recorded below.
- Env: `/etc/crossex-hl.env`, created from
  [`config.hyperliquid.example.env`](config.hyperliquid.example.env).
- `CXA_PEER=hyperliquid`, `CXA_HL_INCLUSION_NS=200000000`.
- `CXA_EXEC_MODE=maker_taker`, `CXA_CLOSE_MODE=taker_taker`.
- `CXA_MAKER_TIMEOUT_MS=8000`, `CXA_MAKER_CANCEL_CUSHION_PPM=500`,
  `CXA_MAKER_REQUOTES=1`.
- `CXA_LATENCY_MODE=pessimistic`.
- `CXA_TRADE_NOTIONAL=250`, `CXA_AVAILABLE_PPM=500000`, `CXA_MIN_NET_PPM=0`.
- All other strategy parameters at the defaults in
  [`STRATEGY_SPEC.md`](STRATEGY_SPEC.md).
- Fees come from the APIs:
  - Binance `commissionRate` with a read-only key;
  - Hyperliquid `userFees` for `CXA_HL_ADDRESS`.

```
basket:      BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,1000PEPEUSDT,SUIUSDT,AVAXUSDT,LINKUSDT,LTCUSDT,ARBUSDT,NEARUSDT,APTUSDT,TIAUSDT,FILUSDT,WLDUSDT,BCHUSDT,UNIUSDT,ENAUSDT,HYPEUSDT,ZECUSDT
commit:      cd6b46beef41be0d9714b55319df96f81790e880 (binary sha256 2ee8fe2810af42fe992369592ac9964f12a14cae2995e80b41cfb4d755efb9c1)
start (UTC): 20261021T101058Z
```

## Gate

`crossex daily-report /data/crossex-hl/*.jsonl*` computes every criterion.
Run it with `/etc/crossex-hl.env` loaded, because replay refuses a journal
whose peer differs from `CXA_PEER`. Score **only** journals from this window
(after the start timestamp below). All criteria must be true:

| Criterion | Threshold |
|---|---|
| Latency mode on every file | `pessimistic` |
| Coverage (sum of run spans) | ≥ 14 days |
| Completed cycles (excluding `missed`) | ≥ 150 |
| Legging rate (cycles with any repair, or `legged_out`) | ≤ 10% |

## Decision rule

- **Pass:** the sample is large enough to evaluate. Go to a live Hyperliquid
  gateway only if the reported mean net edge, and its CI, justify canary
  risk. That gateway needs an agent wallet, ALO/IOC orders, and the
  address-based request budget.
- **Fail:** stop, and write up `census-report` and `daily-report` as the
  result. Do not do any of the following and then re-score the same data:
  - lower the thresholds;
  - narrow the basket to the coins that happened to win;
  - change the inclusion delay;
  - change the entry/exit parameters.

## Reported (not gating)

- Mean net ppm / USD per completed cycle, and the bootstrap 95% CI lower
  bound (10,000 resamples, fixed seed)
- Worst single-cycle loss (USD)
- Realised versus expected net (`mean_expected_net_ppm`)
- Share of cycles where the maker was on Hyperliquid versus on Binance (the
  first `I` open leg of each cycle is the maker)
- Census: episodes per day at ≥30/40/50/75 bp for each size, and the share
  lasting ≥500 ms
- The same journals replayed with `CXA_HL_INCLUSION_NS=50000000` and
  `CXA_LATENCY_MODE=sampled`, to show latency sensitivity
