# Bar Portion perpetual market-making service

## Objective

Build a standalone C++ service for Binance USD-M perpetuals, deployed in AWS
Tokyo, that implements the Bar Portion (BP) market-making strategy described by
Stoikov et al. The first operating mode consumes the live mainnet feed and
simulates queue-aware fills. The same order manager and risk checks will later
drive tightly capped live maker orders without changing the strategy path.

The initial performance contract is **single-digit microseconds for the warm,
in-process path from an already parsed, book-applicable market event to a quote
decision at p99**. WebSocket receipt, TLS, JSON parsing, exchange transit,
order acknowledgement, and fills are measured separately. They cannot be made
single-digit microseconds over Binance's public Internet APIs.

The primary paper (SSRN 5066176) defines the BP signal but does not publish a
complete, reproducible PMM-BP algorithm or its live parameter files. Phase 0
must freeze the missing details in an explicit strategy specification before
strategy implementation begins.

## Primary-paper findings and limits

The paper defines the one-minute Bar Portion signal as

`BP[t] = (close[t] - open[t]) / (high[t] - low[t])`,

with a theoretical range of `[-1, 1]`. It reports a monotonically decreasing
relationship between BP and the following minute's return: strongly positive
bars tend to mean-revert downward, and strongly negative bars tend to
mean-revert upward. The denominator-zero behavior is not specified; the service
specification must define it and test it explicitly.

The paper's directional research uses 45 days of one-minute Binance candles for
30 perpetual-enabled assets. It fits a rolling one-dimensional linear
regression on the preceding 36 days (51,840 observations) and evaluates the
following nine days (reported as 12,959 observations). This directional
experiment does not include trading costs and is not itself the PMM strategy.

For PMM risk calibration, the paper describes two quote levels and reports that
optimized start and step spreads were approximately four to five times monthly
volatility. Stop loss and take profit showed no stable relationship with
monthly volatility and were optimized separately. A separate PEPE PMM-Simple
experiment found an 8-9 minute stop-loss cooldown and 3-5 minute executor
refresh range; those values are not established as PMM-BP parameters.

The approximately 24-hour live comparison used OKX perpetuals, not Binance, on
SOL-USDT, DOGE-USDT, and GALA-USDT. Accounts used 500 USDT quote allocation in
the reported SOL comparison. Sessions were not perfectly synchronized, the
sample was short, and the published screenshots do not expose a complete
parameter set. The backtest used a stated 0.06% trading cost while the live
comparison cites `2 x 0.02% = 0.04%`, excluding slippage.

Consequently, Binance connectivity, fill modeling, latency engineering,
inventory skew, toxicity controls, and the exact BP-forecast-to-reference-price
mapping are extensions required by this project. They must be documented as
such and validated independently; performance figures from the paper are
context rather than acceptance criteria.

## Service boundary

Create `services/bar-portion-mm-service/` as a self-contained CMake project.
The low-latency process owns all state needed to quote safely:

- Binance market-data and user-data WebSockets;
- a verified local L2 book built from a REST snapshot and incremental depth;
- candle aggregation and incremental BP state;
- inventory, open-order, quote, and triple-barrier state machines;
- quote generation, inline risk checks, and order-rate accounting;
- shadow and live venue implementations behind one interface;
- append-only event, decision, order, fill, position, and latency records.

HTTP dashboards, report generation, and research jobs stay outside the hot
path. A small control-plane thread may expose health and metrics from immutable
snapshots. It must not lock or allocate on the decision core.

The service will follow repository conventions established by
`ofi-scalper-service`: raw-frame recording with local receive timestamps,
deterministic replay, gap detection and resynchronisation, queue-aware shadow
fills, durable halts, latency histograms, daily reports, and failure drills.
It will not call the Python Binance plugin or execution service from its hot
path, because serialization, process hops, and Python scheduling would defeat
the latency objective.

## Runtime design

### Threads and data flow

1. A pinned network-ingest thread maintains warm TLS/WebSocket connections,
   timestamps frames with a monotonic clock, and parses only required fields.
2. A pinned decision thread consumes a bounded SPSC ring. It applies the book
   event, updates incremental features and BP state, evaluates risk, and emits
   zero or more quote intents into another bounded SPSC ring.
3. An order-I/O thread signs and submits/cancels live orders or advances the
   shadow venue. It publishes acknowledgements and fills back to the decision
   thread in sequence.
4. A recorder thread writes the raw and normalized journals in batches.
5. A low-priority control thread publishes health, metrics, and operator
   commands from snapshots.

All hot-path structures are fixed-capacity and preallocated after startup.
There is no heap allocation, blocking I/O, logging, mutex contention, DNS,
REST reconciliation, or configuration parsing on the decision core.

### Market data

- Subscribe to Binance USD-M `depth@0ms`, `bookTicker`, aggregate trades,
  mark price/funding, and the candle input required by the frozen BP spec.
- Establish the book from `/fapi/v1/depth`, buffer diffs during the snapshot,
  and enforce Binance `U/u/pu` continuity rules.
- Mark a symbol non-quotable on gaps, crossed/empty books, stale event or local
  receive times, backlog growth, clock discontinuity, or reconnect.
- Cancel resting quotes before resynchronising. Resume only after the book,
  account, positions, and orders have reconciled.
- Rotate WebSockets before Binance's connection lifetime and test seamless
  switchover under replay and on the live feed.

### Strategy

Freeze a versioned strategy specification containing:

- the exact BP equation, candle interval, lookback, normalization, and whether
  the live candle participates;
- how BP shifts reservation price, bid/ask spreads, or both;
- inventory target, skew function, maximum inventory, quote size, quote levels,
  refresh rules, tick rounding, and post-only crossing behavior;
- volatility widening and order-flow toxicity guards;
- stop-loss, take-profit, and time-barrier definitions;
- funding treatment and position carry across funding timestamps;
- paper baseline parameters and each permitted deviation.

Strategy state is updated incrementally. A market event that cannot affect a
quote exits early. A quote is replaced only when the desired price/size differs
materially, the BP regime changes, inventory changes, a barrier fires, or a
staleness/risk rule requires cancellation. This controls needless cancel rate
and avoids deceptive order behavior.

### Shadow and live order paths

`IVenue` exposes the same submit, cancel, amend-if-supported, acknowledgement,
fill, and reconciliation events to the strategy in every mode.

- `ShadowVenue` receives live mainnet events and simulates exchange arrival at
  measured decision-to-venue latency. A new maker order joins behind displayed
  quantity; opposite-side trades deplete the queue; unexplained depth
  reductions use conservative front/back attribution; trade-through fills the
  remainder. It models partial fills, post-only rejects, cancels in flight,
  fees, funding, and market-close slippage through visible depth.
- `LiveVenue` uses Binance's supported authenticated order transport, user-data
  stream, deterministic client IDs, idempotent state transitions, and REST only
  for startup/recovery reconciliation. It is compiled from the beginning but
  startup refuses it unless explicit live gates and hard caps are present.
- During live rollout, the service maintains a counterfactual shadow book for
  every intended order so realized fills can be compared with the fill model.

No mainnet order should be sent during the initial shadow phase. If an
order-path probe is approved, it is a separate mode with a tiny cap and its own
explicit enable flag, not a side effect of shadow mode.

## Risk and failure behavior

Every intent passes synchronous, allocation-free checks for:

- verified and fresh book/account state;
- maximum gross and net inventory per symbol and in aggregate;
- maximum order notional, open orders, leverage, margin utilization, and
  distance from liquidation;
- maximum quote age, unacknowledged order age, and unconfirmed-cancel age;
- exchange request/order limits with headroom and bounded retry behavior;
- daily loss, peak-to-trough drawdown, adverse-selection, and reject-rate caps;
- volatility, spread, price-band, order-flow toxicity, and funding-event guards.

A durable kill state survives restart. Feed gaps, stale books, sequence errors,
unknown order outcomes, user-stream loss, position mismatch, journal failure,
or risk breaches stop new quotes and request cancellation. The system must
distinguish local intent, sent, acknowledged, partially filled, cancel sent,
cancel acknowledged, filled, rejected, expired, and unknown states. Flattening
is explicit and reduce-only; it is never inferred from a failed cancel.

## Delivery phases and gates

### Phase 0: freeze the research specification

- Translate the reviewed primary paper into a complete strategy spec, marking
  every project-defined choice where the paper is silent.
- Write versioned golden examples for BP, reservation price, spread, inventory
  skew, and triple-barrier transitions.
- Select initial symbols, position mode, fee tier, capital and loss caps.
- Record every intentional difference from the paper.

Gate: two independent implementations of the golden vectors agree bit for bit,
and no strategy field remains implicit.

### Phase 1: skeleton and latency harness

- Add CMake presets, compiler warnings, sanitizers, unit tests, benchmarks, and
  release profiles with reproducible dependencies.
- Implement fixed-point price/quantity types, clocks, bounded rings, object
  pools, and HDR-style latency histograms.
- Benchmark an artificial event through book update, BP update, quote math,
  inline risk, and intent emission.

Gate: release build on the target AWS CPU achieves parsed-event-to-intent p99
at or below 9 microseconds under sustained representative load, with zero hot-
path allocations. Record p50/p99/p99.9/max rather than a single best result.

### Phase 2: live feed, recorder, and deterministic replay

- Implement snapshot-plus-diff book recovery, reconnect rotation, trade and
  candle aggregation, raw journals, and normalized event journals.
- Replay recordings through the same book and strategy code with a simulated
  monotonic clock.
- Add disconnect, sequence-gap, duplicate/out-of-order event, backlog, stale
  clock, corrupt journal, and disk-pressure drills.

Gate: a 24-hour Tokyo capture has no unexplained book divergence; every forced
fault pauses quotes; replay produces identical decisions and state hashes.

### Phase 3: BP strategy and shadow fills

- Implement the frozen BP model, quote skew, inventory controller,
  triple-barrier exits, volatility widening, and toxicity guard.
- Implement conservative queue-aware fills and latency injection.
- Attribute P&L into spread capture, BP directional component, adverse
  selection, maker/taker fees, funding, and liquidation/close costs.

Gate: golden vectors, unit/property tests, and replay pass; results remain
acceptable under pessimistic queue attribution, measured latency, 2x latency,
fee-tier changes, and parameter perturbations.

### Phase 4: AWS Tokyo hardening

- Deploy one process per account or isolated symbol group on a fixed EC2
  instance type; benchmark at least two suitable Tokyo instance families.
- Pin threads to physical cores, isolate the decision core where supported,
  lock memory, pre-fault pages, keep the CPU governor stable, enable ENA, and
  tune socket buffers only from measured loss/backlog evidence.
- Use chrony plus the best AWS-supported clock source and record exchange,
  wall, monotonic, and local receive times separately.
- Run under systemd with restart limits, read-only filesystem areas, minimal
  capabilities, encrypted storage, secret injection, log rotation, alarms,
  and an operator runbook.

Gate: 72 hours continuous shadow operation with no unexplained restart, gap,
  state mismatch, missed risk transition, or sustained backlog; latency and
  feed-age SLOs hold at p99 and p99.9.

### Phase 5: forward shadow validation

- Run a preregistered forward period with frozen parameters and no tuning on
  the evaluation interval.
- Compare the paper assets and a liquidity-first control set, one symbol at a
  time before portfolio operation.
- Review fill probability by queue depth, markouts at 10 ms/100 ms/1 s/5 s,
  cancel efficiency, inventory duration, funding, and loss concentration.

Gate: a minimum sample size and calendar duration agreed in Phase 0; positive
net P&L after actual fee tier and funding in the base and pessimistic fill
models; bounded adverse selection and drawdown; all failure drills pass.

### Phase 6: controlled live rollout

- Enable one symbol, one quote level, minimal notional, low leverage, maker-only
  entries, hard daily loss, inventory, order-rate, and total-exposure caps.
- Compare every real acknowledgement/fill with the counterfactual shadow
  event. Reconcile exchange orders, fills, positions, balances, and funding.
- Scale only through explicit gates based on realized fill-model error,
  after-fee P&L, drawdown, adverse selection, order rejects, and operational
  stability. A failed gate returns the service to shadow mode.

Gate: live operation is never enabled merely because shadow P&L is positive.
The operator must explicitly change the mode and limits after the live-readiness
report passes.

## Verification matrix

- Unit/golden: fixed-point arithmetic, BP vectors, rounding, quote skew,
  barriers, inventory, fees, funding, and every risk transition.
- Property/fuzz: WebSocket JSON, depth sequences, duplicate events, order-event
  permutations, partial fills, cancel/fill races, and journal recovery.
- Replay: deterministic decisions and state hashes across normal and fault tapes.
- Simulation: queue models, latency sweeps, rate limits, disconnects, crashes,
  exchange rejects, ambiguous outcomes, position drift, and forced liquidation.
- Performance: isolated microbenchmarks plus full-process load at recorded peak
  message rates, with warm/cold caches and recorder pressure.
- Operations: restart/reconcile, kill persistence, credential rotation, clock
  drift, disk full, memory pressure, and alert delivery.

## Initial artifacts

- `CMakeLists.txt`, dependency lock/manifest, presets, and build container or
  documented Amazon Linux toolchain;
- `include/` and `src/` modules for feed, book, bars/BP, quotes, risk, shadow,
  Binance order I/O, journal, replay, metrics, and control;
- versioned strategy spec and golden vectors;
- replay CLI, latency benchmark, gap checker, daily report, and readiness report;
- systemd unit, AWS bootstrap/deploy instructions, alarms, and runbook;
- README documenting modes, commands, configuration, secrets by name only,
  metrics, recovery, and the live-enable gate.

## Decisions recorded on 2026-10-14

- The BP implementation will use the primary SSRN paper supplied by the user.
  The paper has been reviewed; its incomplete PMM mapping and parameter details
  will be resolved in a versioned strategy specification rather than inferred
  silently.
- The latency acceptance boundary is parsed, book-applicable event through
  quote-intent emission, including book update, BP update, quote calculation,
  and inline risk. The target is p99 at or below 9 microseconds on the selected
  AWS Tokyo instance.
- The first deployment is mainnet market data with shadow fills only. Live
  order entry and order-path probes remain hard-disabled until their later
  gates are explicitly approved.

## Inputs to settle during Phase 0

1. Choose the replication universe and initial caps: the paper's SOL/DOGE/GALA
   set, a liquidity-first BTC/ETH/SOL set, or both as separate cohorts.
2. Confirm Binance account mode, jurisdiction/derivatives eligibility, maker
   fee/rebate tier, position mode, leverage ceiling, and whether the account is
   dedicated to this service.
3. Set the forward-shadow duration/sample gate and maximum capital, daily loss,
   drawdown, and inventory limits for the first live stage.
