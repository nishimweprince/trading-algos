# pump.fun Post-Graduation Scalper

Automated TypeScript trading system that detects pump.fun native graduations,
screens them through a strict guardrail engine, enters small scalp positions
targeting a +50% move, and exits within ~1 second of any trigger.

> **Risk statement.** This strategy operates in an extremely high-risk market
> where roughly 98%+ of tokens fail, many maliciously. The system **defaults to
> paper trading**, enforces hard circuit breakers, and never trades more capital
> than configured limits. **No output of this system is financial advice.**

## Status

**Phases 0–2 complete.** The bot boots, validates config, opens SQLite, wires
Telegram, holds a single-instance lock, streams live pump.fun graduations from
the free PumpPortal feed (confirmed on-chain via the free Helius RPC with
latency logging), and screens each through the guardrail engine, persisting a
verdict row. Execution and risk management land in later phases (see
[Implementation phases](#implementation-phases)).

### Guardrail check status

Hard-fail checks (Section 6.1). "Live" = enforced now on the free tier;
"Pending" = returns `unknown`, which vetoes in live mode (unknowns policy,
Section 6.3) and is logged in paper mode.

| Check | Status | Notes |
| --- | --- | --- |
| H1 mint authority revoked | ✅ live | decoded from mint account |
| H2 freeze authority revoked | ✅ live | decoded from mint account |
| H9 Token-2022 extensions | ✅ live | transfer fee / hook / permanent delegate / default-state / non-transferable |
| H8 serial rugger (blacklist) | ✅ live | mint blacklist; creator history pending |
| H10 circuit breakers | ✅ live | kill-sentinel; full risk manager in Phase 5 |
| H3 LP burned/locked | ⏳ Phase 2b | needs verified PumpSwap pool decoder |
| H5 holder concentration | ⏳ Phase 2b | needs pool-vault exclusion |
| H6 creator holdings | ⏳ Phase 2b | needs creator identification |
| H7 liquidity floor + impact | ⏳ Phase 2b | needs pool reserves |
| H4 sellability (honeypot) | ⏳ Phase 4 | needs sell-simulation swap builder |

The pool-dependent checks are intentionally left `unknown` rather than shipping
an unverified account layout in the module whose whole job is preventing losses.
In live mode they force a veto until implemented — the safe default.

### Detection feeds

| Feed | Cost | Default | Notes |
| --- | --- | --- | --- |
| PumpPortal WS | free | on | Purpose-built `migration` events. Needs no Helius plan. |
| Yellowstone gRPC | paid | off | Lowest latency for live trading. Opt-in via `detector.grpcEnabled` once you have a paid plan + `rpc.primaryGrpc`. |

On-chain confirmation and enrichment use `rpc.primaryHttp` (free Helius tier).
Detection runs entirely on the free tier; gRPC is a drop-in upgrade, not a
prerequisite.

## Requirements

- Node.js 20+ (ESM). Native TypeScript execution via `node --experimental-strip-types`.
- npm.

## Quick start

```bash
cd pump-fun
npm install
cp .env.example .env      # fill in secrets when you have them (optional for paper boot)
npm run typecheck
npm test
npm start                 # boots in paper mode using config.yaml
```

A bare paper boot needs **no credentials**. RPC/streaming endpoints and the
wallet key are only required once detection (Phase 1) and live trading are
enabled.

## Configuration

All tunables live in [`config.yaml`](config.yaml), validated with zod at startup
(Section 9). **Restart to apply — hot-reload is not supported in v1.**

Secrets never live in `config.yaml`:

- The wallet key and Telegram token are referenced **by env-var name**
  (`keypairEnvVar`, `telegramBotTokenEnvVar`) and read from `.env` / the
  environment.
- RPC URLs may embed secrets via `${ENV_VAR}` interpolation.

Point at an alternate config with `CONFIG_PATH=/path/to/config.yaml npm start`.

### Run modes

| Mode      | Behavior                                                        |
| --------- | -------------------------------------------------------------- |
| `paper`   | Default. Never signs or sends. Records everything for tuning.   |
| `dry-run` | Builds and signs real transactions, simulates them, never sends.|
| `live`    | Sends real transactions. Requires `rpc` + `jito` config + wallet key. |

Mode gating is enforced in one place (`executor/broadcaster.ts`, Phase 4) and
covered by tests. No shortcut sends real transactions in paper or dry-run modes.

## Project layout

```
src/
  index.ts              # bootstrap: config, lock, DB, bus, alerts, shutdown
  config/               # zod schema + loader (env interpolation, secret reads)
  core/                 # typed event bus, domain types, program IDs, logger, lock
  persistence/          # SQLite schema + repositories (Section 10)
  alerts/               # Telegram alerter
test/                   # vitest unit tests
```

Later phases add `detector/`, `guardrails/`, `executor/`, `positions/`,
`exits/`, `risk/`, and `replay/` (see the implementation plan).

## Implementation phases

Build in order; each phase ends with tests green and a CHANGELOG entry.

0. **Skeleton** — scaffold, config, bus, SQLite, alerts, lock. ✅
1. **Detection** — gRPC graduation stream + PumpPortal fallback + dedupe + latency.
2. **Enrichment + Guardrails** — Sections 5 & 6.
3. **Pricing + Paper positions** — local pool pricing, position FSM, exit triggers.
4. **Execution** — buy path, pre-signed exit ladder, multi-path broadcaster, Jito, dry-run.
5. **In-position guardrails + Risk manager** — emergency triggers, breakers, kill switch.
6. **Live pilot hardening** — structured logging, crash recovery, runbook.

## Operator warnings (Section 13)

- **Sub-second exit is a target, not a guarantee.** Solana congestion, Jito
  auction losses, or RPC degradation can extend exits to several seconds. The
  slippage ladder exists precisely for this.
- **Guardrails reduce but cannot eliminate rug risk.** Soft rugs (dev dumps from
  unflagged wallets, coordinated holder exits, social abandonment) pass
  structural checks. Expect losses; viability must be proven in paper mode first,
  and past patterns can stop working as the meta shifts.
- **Program interfaces change.** pump.fun / PumpSwap program IDs are pinned in
  `core/constants.ts`, overridable in config, and asserted on-chain at startup
  (from Phase 1). Expect maintenance.
- **External APIs (RugCheck, PumpPortal) rate-limit and go down.** All hard-fail
  logic must be verifiable on-chain; APIs are advisory only.
- **Fees matter.** Priority fees plus Jito tips on both legs can consume a large
  share of a +50% move on small positions. The paper-mode report must include a
  fee-adjusted PnL model before going live.
- **Infrastructure.** For realistic sub-second exits, run on a VPS close to your
  RPC provider's edge (commonly US-East or Frankfurt for Solana infra) with a
  paid low-latency plan.
- **Legal/tax.** Automated trading of these assets may carry regulatory and tax
  implications depending on jurisdiction. The operator is responsible for
  compliance.

## Why every timeout is in minutes/seconds

Empirically (Georgia Tech MemeTrans dataset), for the cohort of coins that pump
after graduating, the whole graduation → peak → collapse lifecycle typically
plays out in under ~20 minutes. That single observation justifies the aggressive
15-minute time stop, the early-arming tight trailing stop, the one-second exit
design, and the 45-second ladder refresh. If the meta shifts to slower pumps,
those config values are what you retune.
