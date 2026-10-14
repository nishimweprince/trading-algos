# Bar Portion market-making service

C++20 Binance USD-M perpetual market maker implementing the Bar Portion signal
from Stoikov et al. (SSRN 5066176). The service is **shadow-only**: it consumes
mainnet public data and simulates fills; every other execution mode is refused.

The paper defines BP but not a reproducible PMM controller. Read
[`STRATEGY_SPEC.md`](STRATEGY_SPEC.md) for the exact boundary between paper
behavior and project choices, and [`PLAN.md`](PLAN.md) for delivery gates.

## Build and test

Dependencies: CMake >=3.25, Ninja, a C++20 compiler, libcurl with `wss`, OpenSSL,
and pthreads.

```sh
cmake --preset release
cmake --build --preset release -j 2
ctest --preset release

cmake --preset sanitizers
cmake --build --preset sanitizers -j 2
ctest --preset sanitizers
```

## Commands

```sh
# Public mainnet feed, verified BTCUSDT book, shadow orders only.
build/release/bar-mm shadow --symbol BTCUSDT \
  --journal data/btcusdt.jsonl --kill-file data/KILL --port 8060

build/release/bar-mm replay data/btcusdt.jsonl
build/release/bar-mm gapcheck data/btcusdt.jsonl
build/release/bar-mm daily-report data/btcusdt.jsonl
build/release/bar-mm benchmark
build/release/bar-mm drill
build/release/bar-mm calibrate research/bp_returns.csv
```

Calibration CSV contains `bp,next_minute_return`, one pair per line. Preparing
those pairs must respect the timestamp rules in the strategy specification.

The status server binds only to `127.0.0.1`: `/health/live`, `/health/ready`,
and `/metrics` return the allowlisted shadow status JSON. A kill file cancels
all shadow quotes and survives restart. Remove it to allow the process to
acknowledge the halt after the book is verified again.

`--mode live`, or any value other than `shadow`, exits before opening a network
connection. The service contains no API-key configuration and makes only public
snapshot and WebSocket requests.

## Journals and replay

Each line is an append-only snapshot (`S`), raw frame (`R`), normalized depth
(`D`), trade (`T`), closed/open candle (`K`), quote intent (`I`), or shadow fill
(`F`). Local receive nanoseconds and exchange timestamps are retained. Replay
consumes normalized records through the production book, strategy, risk, and
shadow engine and reports a deterministic state hash.

The raw and normalized forms intentionally coexist: raw frames support parser
revalidation while normalized records keep deterministic research independent
of later parser changes. Journals, build trees, credentials, and local config
are ignored by Git.

In shadow mode, the network callback timestamps and parses frames into a bounded
single-producer/single-consumer queue. A dedicated decision thread owns the
book, strategy, risk, and shadow venue. A separate recorder thread drains
bounded raw and normalized queues. Any queue overflow or journal write failure
halts the pipeline instead of dropping state silently.

## Latency contract

`bar-mm benchmark` measures parsed, book-applicable event through book update,
strategy, inline risk, and intent emission. It reports p50/p99/p99.9/max and
fails unless p99 is at most 9 microseconds with zero allocations in the measured
region. This excludes TLS/WebSocket parsing, queues, signing, network transit,
acknowledgements, and fills. Final acceptance must run on the selected AWS Tokyo
instance using the recorded production compiler and CPU configuration.

## Operations

See [`docs/aws-tokyo.md`](docs/aws-tokyo.md) for host preparation and
[`RUNBOOK.md`](RUNBOOK.md) for startup, gaps, kills, disk faults, and recovery.
The systemd unit lives at `infra/systemd/bar-portion-mm.service`.
