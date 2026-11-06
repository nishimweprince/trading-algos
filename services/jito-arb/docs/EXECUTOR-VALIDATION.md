# Local executor validation — 6 November 2026

Agave 4.4.1, platform-tools 1.57; executor compiled to SBF. Public mainnet accounts were cloned into an isolated localhost ledger. All transactions were unsigned simulations with signature verification disabled. No transaction was submitted to mainnet and the executor was not deployed there.

The validator-only build replaced the executor OWNER constant with the public holder `2yik7ZsLfFKAmmr9cCHhL1dTabiECnFKDeSVyTbxeETU`; the source was restored afterward. ProgramData deployment slots were rebased to zero in cloned files. The runner waits for processed slot 2 so the v0 address loader has slot-history sysvars.

A two-leg round trip through each pool validates both directions and measured-output chaining. Each baseline was repeated with min_out equal to its output (success) and output plus one (executor custom error 6002).

- CPMM: input 1,000,000, final output 995,004; 75,244 CU; acceptance passed.
- PumpSwap: input 1,000,000, final output 994,012; 135,582 CU; acceptance passed.
- CLMM: input 1,000,000, final output 999,182; 128,199 CU; acceptance passed.
- DLMM: pending parity and cloned-validator execution.

PumpSwap exposed a holder volume accumulator below the local rent threshold. Diagnostic assembly now initializes a missing accumulator or funds its rent deficit from the public payer inside the unsigned simulation. This setup does not mutate mainnet. Direct-leg simulations retain fixed quoted inputs and remain distinct from this executor test.

VM artifacts: `/tmp/jito-validator-{cpmm,pumpswap,clmm}/result.json`, `probe-*.log`, and cloned account JSON; verification checkout `/tmp/jito-coverage-verify`. Re-run with `program/executor/tests/cloned_validator.py` and a manifest captured by `executor-probe --manifest`.
