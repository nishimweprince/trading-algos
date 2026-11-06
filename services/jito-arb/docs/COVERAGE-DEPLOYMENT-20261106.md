# Coverage implementation and deployment — 6 November 2026

## Running deployment

Frankfurt runs `67bd7c0ba452aeb2f0ea4179bce41932a730fd8d`, verified against the running process's executable hash. The command line still forces `--dry-run`. No mainnet transaction was submitted and no executor was deployed to mainnet.

All four added DEXes are explicitly enabled in `/etc/jito-arb/bot.toml`. The final discovery contains 1,500 pools and 44,085 cycles:

- Raydium v4: 791 pools.
- Whirlpool: 103 pools.
- CPMM: 114 pools.
- PumpSwap: 178 pools.
- CLMM: 102 pools.
- DLMM: 212 pools.

`pool-inventory` checks the exact StateStore account set before installation: 8,707 pool keys, six pool streams and one wallet/reserve stream, zero overflow. Bot, dashboard and discovery timer are active. Recorder remains inactive, preserving its previous state. Dashboard stays on `127.0.0.1:8088`; metrics stay on `127.0.0.1:9100`.

Per-DEX tracked, complete, ready, supported and unsupported gauges are separate, alongside explicit enablement gauges. Readiness varies during updates; equality with tracked pools is not an acceptance invariant. DLMM's initial supported subset is classic SPL tokens. Token-2022 DLMM pools are explicitly refused.

The machine-readable receipt is [evidence/coverage-20261106.json](evidence/coverage-20261106.json). It includes the actual running binary hash, service status, metric snapshot, sample counts and observation evidence.

## Verification

- Integration: 104 C++ tests pass on the laptop and VM.
- Standalone phase branches: 57 / 66 / 72 / 78 / 91 / 104 tests for phases 0–5.
- Seven deployment tests verify archive uniqueness, protected files, writer-stop failures, recorder restoration and installation preservation.
- Six dashboard tests verify incremental reads, outcome joins, setup/route distinctions, partial writes, resets, repeated signatures, time windows and bounded memory with 50,000 skip records.
- Exact chain parity: CPMM 260, PumpSwap 530, CLMM 256 and DLMM 246, with zero mismatches. Moving or unverified samples are excluded.
- All four executor arms pass opposing-leg swaps on an isolated Agave 4.4.1 validator, measured-output chaining, equal `min_out` success and `min_out + 1` rejection. See [EXECUTOR-VALIDATION.md](EXECUTOR-VALIDATION.md).
- Live quote ladders exercised three pools per added DEX. A transient DLMM clock-coherence rejection passed on retry; very large sizes can still report liquidity/cache boundaries.

The simulation worker uses public seeded holders, static unsigned direct-leg messages, live funding validation and bounded queues/results. Setup, wallet movement, consumed inventory, oversized messages and RPC failures are distinguished from failed swap legs. Minimum-context errors receive at most two 100 ms retries on the background worker, preserving the same required slot and balance baseline.

## Operational findings

The initial repaired worker was observed for 3,632 seconds: three signed jobs, three terminal outcomes, no pending work or sampling drops. Two failed swap routes were manually inspected; the other result was an RPC minimum-context error from a simulation bank one slot behind the requested context.

A subsequent fresh production candidate simulated successfully: Raydium v4 → Whirlpool, signature `51NboCgkGaJLTt2XW6SinMYXDjeJpvqW5u6HFPCb69CYSTJbtHxBsifd1ddvgo9uAzXWkzmfYfrQMsWD9HhCKkeX`, net **−821,930 lamports**, leg CU 15,404 / 43,952. Success means the legs ran; it does not mean the route would profit. Two separate archived-input/current-state replay successes are retained as diagnostic evidence, not historical validation.

Expanded coverage generated natural CLMM/DLMM candidate routes. Before log-rate protection, 17 signed jobs reconciled to 17 terminal outcomes: ten oversized static messages and seven RPC errors. In the next run, ten signed jobs reconciled to ten outcomes, including two CLMM route failures that reached chain simulation. These are preserved separately in the receipt. After bounded context retries were installed, another seven signed jobs reconciled to seven terminal outcomes: six oversized static messages and one CLMM/Raydium route failure that reached chain simulation. Representative successful live routes for every added DEX remain an observation gate; parity and validator tests do not replace it.

The expanded feed also exposed unbounded dashboard allocation and excessive skipped-decision logging. The dashboard now reads only appended records, retains second-resolution aggregates and 60 recent rows, and joins signed outcomes separately. Explicit production `obs.skip_log_max_per_s = 20` bounds only skipped-record logging. **Every signed candidate and outcome is still retained.** The UI labels logged candidates and exposes the sampling counter. Observed UI RSS fell from several GB to about 30 MB; `/api/decisions` returned HTTP 200 in about 6 ms. Visual browser QA was unavailable because local browser use is disabled in app settings; HTTP and unit checks passed.

## Archives and operator files

Each installation ran `install.sh`, then `reset-stats.sh`. Protected pools, denylist, KILL and environment contents were checked during reset; configuration changes were applied explicitly before installation. No environment secrets were changed.

Relevant unique UTC archives under `/var/lib/jito-arb/archive/`:

- Phase 3: `20261106T111603Z.VZGPJS`.
- Phase 4: `20261106T111829Z.DwhWjL`.
- Initial phase 5: `20261106T111850Z.JbXX3T`.
- Log-volume/dashboard repair: `20261106T113037Z.EyiDph` (including the 1.2 GB unsampled log).
- Bounded context-retry deployment: `20261106T113514Z.L86iUf`.
- Final dashboard accounting deployment: `20261106T114113Z.dCi4Cj`.

Receipts and metric snapshots are stored inside deployment archives. Independent observation files remain under `/var/lib/jito-arb/sim-validation/`.

## Phase 6 gate

The requested `SubscribePreprocessed` gRPC method returned **UNIMPLEMENTED (12)** immediately on the configured Frankfurt endpoint, including a request filtered to the 1,047 pools tracked at probe time. No messages or lag samples were available. Phase 6 stopped at this required availability gate. There is no WebSocket substitution, shadow overlay, dispatch integration or claimed one-hour shadow observation.

## PR stack

Prerequisites: [#76](https://github.com/nishimweprince/trading-algos/pull/76). Phase 0: [#77](https://github.com/nishimweprince/trading-algos/pull/77). Phase 1: [#78](https://github.com/nishimweprince/trading-algos/pull/78). CPMM: [#79](https://github.com/nishimweprince/trading-algos/pull/79). PumpSwap: [#80](https://github.com/nishimweprince/trading-algos/pull/80). CLMM: [#81](https://github.com/nishimweprince/trading-algos/pull/81). DLMM and load safeguards: [#82](https://github.com/nishimweprince/trading-algos/pull/82). Preprocessed availability gate: [#83](https://github.com/nishimweprince/trading-algos/pull/83).
