## P1 gate status

| Gate criterion | Status |
|---|---|
| Re-costed numbers reproducible (same seed → same bytes) | **Met.** `test/research-stats.test.ts` asserts the −0.62 SOL total; running the CLI twice produces identical files. |
| Fee model matches expectation (≈ −0.62 SOL on the baseline week) | **Met.** −0.617 SOL. |
| Live-vs-paper delta < 3 %/trade | **Open. The data needed does not exist yet.** |

**Why the delta criterion is still open.** The stop-loss rows above compare like with like, and they already agree within 0.3 pt once real fees are charged (live −24.4 % vs paper −24.1 %). The −10.3 pt overall gap comes from the exit **mix**: live had 0 take-profits in 18 trades. That mix is a population and latency effect, and a flat re-cost of old paper rows cannot reproduce it. The honest simulator (`simulator.enabled: true`) models it directly: latency before entry, worst-in-window stops, and failed entries. Its output does not exist until it has run.

Two steps close the gate (see `reports/RUNBOOK-edge-plan.md`):
1. Run 48 h of dry-run with the simulator on. Every paper row is then `simulated=1`, with a simulated `exit_trigger_to_confirm_ms`.
2. In the P4.2 twin pilot, `getExecutionDragComparison` (dashboard → Execution drag) reports the per-trade live-vs-twin Δ directly. The gate is Δ < 3 %/trade.
