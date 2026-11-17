# Local executor validation — 6 November 2026

Agave 4.4.1, platform-tools 1.57; executor compiled to SBF. Public mainnet accounts were cloned into an isolated localhost ledger. All transactions were unsigned simulations with signature verification disabled. No transaction was submitted to mainnet and the executor was not deployed there.

The validator-only build replaced the executor OWNER constant with the public holder `2yik7ZsLfFKAmmr9cCHhL1dTabiECnFKDeSVyTbxeETU`; the source was restored afterward. ProgramData deployment slots were rebased to zero in cloned files. The runner waits for processed slot 2 so the v0 address loader has slot-history sysvars.

A two-leg round trip through each pool validates both directions and measured-output chaining. Each baseline was repeated with min_out equal to its output (success) and output plus one (executor custom error 6002).

- CPMM: input 1,000,000, final output 995,004; 75,244 CU; acceptance passed.
- PumpSwap: input 1,000,000, final output 994,012; 135,582 CU; acceptance passed.
- CLMM: input 1,000,000, final output 999,182; 128,199 CU; acceptance passed.
- DLMM: input 1,000,000, final output 997,984; 102,736 CU; acceptance passed.

DLMM parity: 246 exact verified samples in 13 unchanged-state batches, zero mismatches. Five moved batches and 14 unverified samples were discarded; both directions were exercised. Initial enablement remains limited to supported classic SPL token pools; Token-2022 DLMM pools are explicitly rejected.

PumpSwap exposed a holder volume accumulator below the local rent threshold. Diagnostic assembly now initializes a missing accumulator or funds its rent deficit from the public payer inside the unsigned simulation. This setup does not mutate mainnet. Direct-leg simulations retain fixed quoted inputs and remain distinct from this executor test.

VM artifacts: `/tmp/jito-validator-{cpmm,pumpswap,clmm}/result.json`, `probe-*.log`, and cloned account JSON; verification checkout `/tmp/jito-coverage-verify`. Re-run with `program/executor/tests/cloned_validator.py` and a manifest captured by `executor-probe --manifest`.

## Whirlpool, Raydium v4 and the Kamino round trip (17 November 2026)

Same method on the Frankfurt VM (Agave 4.4.1, executor rebuilt with the validator-only OWNER
`2yik7ZsLfFKAmmr9cCHhL1dTabiECnFKDeSVyTbxeETU`, restored afterwards; a throwaway executor id). Each pool: a two-leg
round trip of 1,000,000 lamports of wSOL, once with min_out = output (success) and once with output + 1 (custom
error 6002). `--flash` wraps the executor in a Kamino Main Market wSOL flash borrow and repay
(`d4A2prbA2whesmvHaL88BH6Ewn5N4bTSU2Ze8P6Bc4Q`), the §7.3 layout without the tip.

| Pool | DEX | Plain: output, CU | Flash: output, CU | min_out + 1 reverts (6002) |
| --- | --- | --- | --- | --- |
| Czfq3xZZ… (SOL/USDC, 405k SOL TVL) | Whirlpool | 999,189, 84,749 | 999,198, 146,182 | both |
| B5EwJVDu… (armed) | Whirlpool | 989,048, 70,767 | 988,773, 132,193 | both |
| 58oQChx4… (SOL/USDC, armed) | Raydium v4 | 994,990, 57,775 | 994,983, 118,806 | both |
| Bzc9NZfM… (armed) | Raydium v4 | 995,005, 53,651 | 995,005, 114,689 | both |

The flash borrow and repay add about 61,000 CU. Outputs differ slightly between runs because each run clones the
pool at a later slot. The first flash pass reported the min_out + 1 run as succeeding: the probe measured the
whole transaction, which includes the flash fee repaid after the executor's check, so its "output" was 10 to 11
lamports below what the guard compares. `executor-probe --flash` now adds the fee back; the executor itself was
right in both passes. Artifacts: `/tmp/jito-exec-1117/run-*/` on the VM.
