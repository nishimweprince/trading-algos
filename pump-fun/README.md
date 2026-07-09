# pump.fun Post-Graduation Scalper

Automated TypeScript trading system that detects pump.fun native graduations,
screens them through a strict guardrail engine, enters small scalp positions
targeting a +50% move, and exits within ~1 second of any trigger.

> **Risk statement.** This strategy operates in an extremely high-risk market
> where roughly 98%+ of tokens fail, many maliciously. The system **defaults to
> paper trading**, enforces hard circuit breakers, and never trades more capital
> than configured limits. **No output of this system is financial advice.**

## Status

**Phases 0–6 implemented; funded live pilot verification remains.** The bot
boots, validates config, opens SQLite, wires Telegram, holds a single-instance
lock, streams live pump.fun graduations from PumpPortal plus Helius WS
redundancy, screens each through the guardrail engine, runs risk breakers, and
opens positions according to mode.

Live mode no longer treats a submitted signature as a fill: it persists
`PENDING_ENTRY`, waits for confirmed entry execution, reconciles the wallet's
actual token balance, builds a pre-signed exit ladder from that balance, and
only then marks the position `OPEN`. Jito is the primary live send path, with
normal RPC paths kept as fallback. Live exits now persist `EXITING` intents
before broadcasting, retry/escalate until the wallet balance reconciles, and
keep new entries blocked via the kill switch if an exit becomes ambiguous.
Crash recovery resumes unfinished exits before rehydrating live open positions
or allowing new detection to open trades.

### Guardrail check status

Hard-fail checks (Section 6.1). "Live" = enforced now on the free tier;
"Pending" = returns `unknown`, which vetoes in live mode (unknowns policy,
Section 6.3) and is logged in paper mode.

| Check | Status | Notes |
| --- | --- | --- |
| H1 mint authority revoked | ✅ live | decoded from mint account |
| H2 freeze authority revoked | ✅ live | decoded from mint account |
| H3 LP burned/locked | ✅ live | lp_mint circulating supply == 0 (verified PumpSwap pool decoder) |
| H5 holder concentration | ✅ live | top-10 / single caps, pool vaults + burn excluded |
| H6 creator holdings | ✅ live | dev (coin_creator) holdings vs cap |
| H7 liquidity floor + impact | ✅ live | SOL reserve floor + constant-product buy impact |
| H8 serial rugger (blacklist) | ✅ live | mint + creator blacklist; launch-history heuristic later |
| H9 Token-2022 extensions | ✅ live | transfer fee / hook / permanent delegate / default-state / non-transferable |
| H10 circuit breakers | ✅ live | kill switch, stream-down, wallet floor, daily loss, consecutive losses, emergency-exit count |
| H4 sellability (honeypot) | ✅ live* | atomic buy+sell simulation; *conclusive pass/fail needs a funded wallet (dry-run/live) |

All 10 hard checks are wired. H4 still needs a funded persistent wallet to be
conclusive; an unfunded/ephemeral dry-run wallet reports `unknown`, which vetoes
in live mode under the unknowns policy. The PumpSwap pool layout was verified
against live pool accounts before any check trusted it.

### Detection feeds

| Feed | Cost | Default | Notes |
| --- | --- | --- | --- |
| PumpPortal WS | free | on | Purpose-built `migration` events. Needs no Helius plan. |
| Helius WS logs | free with Helius key | on in config | Direct on-chain redundancy; mint lookup adds a tx fetch, so it is not always lower latency than PumpPortal. |
| Yellowstone gRPC | paid | off | Latency upgrade, not required for v1 live execution. Opt-in via `detector.grpcEnabled` once implemented + `rpc.primaryGrpc`. |

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

### Operator Dashboard & Alerts

A read-only web dashboard is available for non-technical operators. It shows
realized PnL from persisted positions, open exposure, recent positions,
guardrail outcomes, operator events, notifications, and basic system health.

The live performance and monitoring dashboard can be found at [pumpdesk.nishimweprince.dev](https://pumpdesk.nishimweprince.dev).

To run the local dashboard:
```bash
npm run dashboard:build
# set dashboard.enabled=true in config.yaml, then:
npm start
```

By default it binds to `127.0.0.1:8787` and is disabled. If you bind it to any
non-localhost host, set both `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD`; the
server refuses exposed dashboard bindings without Basic Auth.

During UI development, run `npm run dashboard:dev` and keep the bot dashboard
server running on port 8787 so Vite can proxy `/api` requests.

#### Alert Routing

Alerts are dispatched via a typed event bus and routed based on the recipient:
- **Telegram Alerts**: Opt-in only (using the `telegram: true` flag on the alert payload). Position changes (opening, exit trigger, and closure) are forwarded to Telegram to keep the operator informed of high-impact events.
- **Dashboard Notifications**: Receives all alert events, including system startup and general notifications.

#### Analytics & reports

The dashboard is operator-first: risk snapshot, unrealized PnL, detection/exit latency percentiles, funnel, and breaker history sit next to realized PnL.

**API (auth same as dashboard):**

| Endpoint | Purpose |
| --- | --- |
| `GET /api/dashboard/summary` | KPIs including expectancy, drawdown, fees, unrealized, latency |
| `GET /api/risk/status` | Live risk counters (requires bot process, not standalone) |
| `GET /api/analytics/ops` | Ops package JSON |
| `GET /api/analytics/performance?range=24h\|7d\|30d\|all` | Edge stats + exit-reason breakdown |
| `GET /api/analytics/funnel?range=…` | Graduation → accept → enter → close |
| `GET /api/reports/trades.csv?range=…` | Trade blotter download |
| `GET /api/reports/ops.json` | Ops health snapshot |
| `GET /api/reports/soak.json?range=…` | Fee-aware paper-soak package |
| `GET /api/reports/funnel.csv?range=…` | Hard-check fail rates |

**Headless soak report** (writes under `reports/`):

```bash
npm run report:soak -- --range 7d --out reports/
```

Use `soak.json` before enabling live: confirm fee-adjusted net PnL, expectancy, profit factor, max drawdown, sample size, and exit-reason mix.

### Run modes

| Mode      | Behavior                                                        |
| --------- | -------------------------------------------------------------- |
| `paper`   | Default. Never signs or sends. Records everything for tuning.   |
| `dry-run` | Builds and signs real transactions, simulates them, never sends.|
| `live`    | Sends real transactions through Jito primary + RPC fallback. Requires `rpc` + `jito` config + wallet key. |

Mode gating is enforced in one place (`executor/broadcaster.ts`) and covered by
tests. No shortcut sends real transactions in paper or dry-run modes. Live
position accounting uses confirmed transactions and reconciled wallet token
balances, not optimistic paper fills.

### Live Gate

Before setting `mode: live`, complete this checklist:

- Funded dry-run with the real wallet key succeeds and H4 sellability produces
  conclusive pass/fail results.
- Jito block-engine URL is reachable, tip accounts can be fetched, and dynamic
  tip-floor fetch falls back safely when unavailable.
- Primary RPC, secondary RPC, PumpPortal, and Helius WS are healthy from the
  deployment host.
- Program IDs are verified by startup assertion and manually checked against a
  block explorer before the pilot.
- Crash recovery has been tested with an open persisted position and real wallet
  token balance.
- Durable exit recovery has been tested with an `EXITING` row, including both
  zero-balance finalization and nonzero-balance retry/kill-switch behavior.
- Telegram `/kill`, KILL file, `/status`, and dashboard auth are verified.
- Pilot config uses `risk.maxConcurrentPositions: 1`, reduced `entry.baseSizeSol`,
  and a hot wallet funded only with loss-tolerant capital.

## Project layout

```
src/
  index.ts              # bootstrap: config, lock, DB, bus, alerts, recovery, shutdown
  config/               # zod schema + loader (env interpolation, secret reads)
  core/                 # typed event bus, domain types, program IDs, logger, lock
  persistence/          # SQLite schema + repositories (Section 10)
  detector/             # PumpPortal + Helius WS feeds, dedupe, confirmation
  enrichment/           # on-chain candidate data + advisory signals
  guardrails/           # hard checks + soft scoring
  executor/             # wallet, PumpSwap SDK, Jito/RPC broadcast, fees
  positions/            # paper/live lifecycle, pricing, pre-signed exit ladder
  exits/                # exit trigger engine
  risk/                 # circuit breakers + kill switch
  alerts/               # Telegram alerter
test/                   # vitest unit tests
```

## Implementation phases

Build in order; each phase ends with tests green and a CHANGELOG entry.

0. **Skeleton** — scaffold, config, bus, SQLite, alerts, lock. ✅
1. **Detection** — PumpPortal + Helius WS, dedupe, confirmation, latency. ✅
2. **Enrichment + Guardrails** — Sections 5 & 6. ✅
3. **Pricing + Paper positions** — local pricing, FSM, exit triggers. ✅
4. **Execution** — PumpSwap SDK swaps, dry-run/live broadcaster, Jito primary + RPC fallback, H4 sellability. ✅
5. **In-position guardrails + Risk manager** — emergency triggers, breakers, kill switch. ✅
6. **Live pilot hardening** — confirmed fill reconciliation, pre-signed live exits, crash recovery, pilot runbook. ✅ implementation complete; requires funded dry-run/live pilot verification.

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
