# Cross-exchange arbitrage service (Binance ↔ Bybit)

A C++20 service that trades the basis between Binance USDⓈ-M and Bybit
linear USDT perpetuals, taker/taker IOC, on pre-funded accounts. It follows
[`deep-research-report-crossex.md`](deep-research-report-crossex.md).
[`PLAN.md`](PLAN.md) has the milestones, [`STRATEGY_SPEC.md`](STRATEGY_SPEC.md)
the exact formulas, and [`PREREG.md`](PREREG.md) the Phase 1 → Phase 2 gate.

**Phase 1 (this code) is shadow-only.** It consumes mainnet public data, reads
the account's real taker fees with **read-only** keys, measures signed-request
RTT, and simulates every leg as a latency-delayed IOC that sweeps the book as
of its arrival time. It has no order-entry code. `--mode` anything other than
`shadow` exits before touching the network, and keys that can trade, transfer
or withdraw are refused at startup. Phase 2 (live taker/taker IOC) is built
only after the PREREG gate passes.

## Build and test

Dependencies: CMake ≥ 3.25, Ninja, a C++20 compiler, OpenSSL, and libcurl
with WebSocket support (`curl-config --protocols` must list `WSS`). The macOS
system libcurl lacks it; CMake picks up Homebrew curl automatically.

```sh
cmake --preset release && cmake --build --preset release -j
ctest --preset release
cmake --preset sanitizers && cmake --build --preset sanitizers -j && ctest --preset sanitizers
```

## Commands

```sh
crossex probe --duration-seconds 86400      # feed age + public/signed RTT, both venues
crossex check-keys                           # read-only verification + account taker fees
crossex shadow                               # main service (config from CXA_* env)
crossex shadow --public-only --duration-seconds 60   # no keys: configured fees, fixed latency
crossex replay <journal>                     # deterministic replay -> state hash
crossex gapcheck <journal>                   # book continuity, gaps vs recoveries
crossex daily-report <journal>...            # cycle P&L and the PREREG gate
crossex census-report <journal>...           # executable-gap frequency and persistence
crossex drill | benchmark                    # failure drills; decision-path latency
```

Reports read `.jsonl` and `.jsonl.zst` files.

## Threads

```
binance thread (/public depth + /market markPrice) ─┐ SPSC
bybit thread (orderbook.50 + tickers) ──────────────┼──▶ decision thread ──SPSC──▶ recorder
aux thread (signed RTT probes, hourly fee refresh) ─┘   (books, edge, census,
                                                          shadow venue, pairs, risk)
status thread: 127.0.0.1:8070  /  /api/summary  /api/activity  /health/*
```

The decision thread merges its three inputs in receive-time order. If a queue
overflows, the journal fails to write, or a frame is malformed, the pipeline
stops and the process exits non-zero so systemd restarts it. On every stop,
open shadow pairs are marked out at the book (`shutdown`).

## Journal

There is one file per run (`CXA_JOURNAL` supports strftime, UTC). Records are
space-separated, one letter per kind:

| Kind | Content |
|---|---|
| `Z` | Header |
| `Y` | Instrument |
| `C` | Taker fee |
| `L` | RTT sample |
| `S` | Binance snapshot |
| `D` | Depth diff or Bybit snapshot |
| `M` | Funding / mark |
| `R` | Raw frame (`--record-raw`) |
| `I` / `F` | Leg submit / leg result |
| `X` | Entry decision with edge breakdown |
| `P` | Closed cycle |
| `O` | Census episode |
| `G` | Resync |
| `H` | Halt change |

Replay consumes only `Y C L S D M`, through the production engine.

## Measured locally (Apple M-series, Kigali)

- `benchmark`: decision path p50 1.5 µs, p99 4.2 µs, zero allocations.
- 90 s live public-only run, 6 symbols: about 30k book events, 0 gaps on
  either venue.
- Journal: about 9.7 MB per 90 s for 6 symbols uncompressed, about 2.3 MB
  with zstd.

Operations: [`RUNBOOK.md`](RUNBOOK.md) and [`docs/aws-tokyo.md`](docs/aws-tokyo.md).
