# Live readiness plan and funding (10 November 2026)

Goal: a small own-capital live test that measures the one thing dry run cannot, the Jito landing rate, with
losses bounded by design. Everything below stays in dry run until Stage 4; the owner makes the switch.

## Where the bot stands

Measured on the Frankfurt VM, 13:01 to 16:51 UTC (3.8 h), 8,000 pools from 15:21.

| Measure | Value |
| --- | --- |
| Dry-run bundles simulated | 244 |
| Fresh (simulated within 20 slots) and passing the guard | 48, net +0.0194 SOL |
| Of those, SOL-base routes | 40 of 48 |
| Trade size on passing SOL routes | p50 0.054 SOL, p90 1.30 SOL, max 2.35 SOL |
| Tip per passing bundle | p50 0.00008 SOL, max 0.0029 SOL |
| Kamino flash fee | 0.1 bps of the amount borrowed |
| 3-hop share of passing routes | 36 of 48 |
| Pools on passing routes / on all simulated routes | 60 / 151 |
| Detect to signed | 11 ms to 73 ms p50, 100 ms p99 after the settle change (one engine thread, at 100% to 120% CPU) |

So the measurable edge is about 0.005 SOL an hour before competition. The live test is an experiment, not income.

## What the code does not do yet

Read from `src/app/live.cpp`, `src/app/bundle_builder.cpp`, `src/submit/jito.{hpp,cpp}`, `src/tx/alt_manager.hpp`,
`src/risk/limits.hpp` and `program/executor/src/processor.rs`.

| Piece | State | Gap |
| --- | --- | --- |
| Executor program | 29,832-byte SBF build; 4 DEX arms pass on a local validator | `OWNER` is a placeholder (all zeros). Raydium v4 and Whirlpool arms have no validator run recorded. Never audited or deployed. |
| On-chain guard | `min_out` check after the last leg | Sound, but only the base ATA is measured: the tip is native SOL, so the builder must keep it inside `min_out` (spec 6.8). Needs a test that fails if it does not. |
| Jito client | `JitoClient`, `Submitter`, `BundleTracker` exist with mocked tests | None is constructed in `live.cpp`: `Submitter(true, nullptr)`. No landing feedback reaches the tip controller, so `p_land` stays at 0.1. |
| Tip accounts | Static default list | `getTipAccounts` refresh not wired. |
| Lookup tables | `AltManager::plan_extend` builds the instructions | Nothing sends them; live uses virtual tables. 36 of 48 passing routes are 3-hop and cannot fit without real tables. |
| Wallet state | `set_wallet_lamports`, `set_own_balance` exist | Never called: the float check and own-capital sizing see no balance. |
| Intermediate token accounts | Executor needs one ATA per intermediate mint | Nothing creates them. 6,167 distinct non-base mints are tracked. |
| Kill switch | KILL file checked before every build | Works in dry run; untested with a real submitter. No Telegram `/stop`. |
| Sweep | `Limits::sweep_excess` | No job unwraps profit (wSOL) back to the SOL float or moves the excess to the cold wallet. |
| Dry-run refusal | `live.cpp:54` returns 2 on `dry_run = false` | Stays until the owner removes it. |

## Why the live universe must be a subset

Tracking 8,000 pools is free. Trading them is not: every pool traded needs lookup-table entries and every
intermediate mint needs a token account, both rent-funded.

| For all 8,000 pools | Count | Rent locked |
| --- | --- | --- |
| Lookup-table entries (45,801 keys, 256 per table) | 179 tables | 10.4 SOL |
| Token accounts (6,167 mints x 0.00204 SOL) | 6,167 | 12.6 SOL |

Only 151 pools appeared on any simulated route in 3.8 h, and 60 on a passing one. So the bot keeps observing
8,000 pools but is *armed* on a short list: pools that appeared on a guard-passing dry-run route in the last
24 h, capped at 250. A candidate that touches an unarmed pool is logged as `not_armed`, which also tells us
what the cap costs.

## Stages

Each stage ends with a check that must pass before the next. Stages 0 to 3 are dry run.

**Stage 0. Make the measurements trustworthy (1 to 2 days)**
- Find why one engine thread sits at about 100% CPU and the newest processed update is about 90 ms old at
  8,000 pools; fix it or cap the armed set's cycles first in the loop. `arb_eval_to_signed_us` (added today)
  splits queueing from pricing.
- Simulate the *executor* transaction, not direct swap legs, once the program exists on chain (Stage 2): that
  gives the real guard verdict and real compute units.
- Exit: detect-to-signed p99 under 10 ms on armed routes; guard pass rate and fresh net reported per day.

**Stage 1. Wire the send path behind the refusal (2 to 3 days)**
- Construct `JitoClient` and `BundleTracker` in `live.cpp` only when `dry_run = false` (unreachable today).
  Landing results feed `TipPolicy` and `p_land`.
- Refresh tip accounts hourly with `getTipAccounts` (read-only, can run in dry run now).
- Wallet stream: feed `set_wallet_lamports` and `set_own_balance` from the wallet subscription.
- Armed set: persisted list, `not_armed` reason, dashboard panel.
- Lookup-table and token-account planner: a dry-run tool that prints the exact tables, accounts, rent and
  transactions the armed set needs. It sends nothing.
- Limits for the test: `max_daily_fee_spend_sol = 0.02`, `max_bundles_per_slot = 1`, own-capital mode,
  `max_trade_lamports = 0.5 SOL`, SOL base only, 2-hop and 3-hop, Sender path off.
- Exit: full unit coverage with mocked Jito; a replay shows the same decisions as today.

**Stage 2. Executor on chain (owner, 1 day)**
- Owner sets `OWNER` to the hot wallet's pubkey, builds, and audits the 180 lines of `processor.rs`.
- Validator runs for Raydium v4 and Whirlpool (the two arms with none recorded), plus the tip-inside-`min_out`
  test and a Kamino borrow/repay round trip.
- Owner deploys with a separate upgrade-authority key. Nothing trades yet: the bot simulates executor
  transactions against the real program.
- Exit: simulated executor outcomes match quotes to the unit on at least 200 routes; zero guard false passes.

**Stage 3. Fund and arm (owner, half a day)**
- Owner creates the hot wallet, funds it, and runs the planner's transactions (tables, token accounts, wSOL).
- KILL file tested with the real submitter still in dry run.

**Stage 4. Live, own capital (owner flips the switch)**
- Owner removes the refusal in a reviewed commit and sets `dry_run = false`.
- Spec phase-4 exit: at least 20 landed bundles, realized output within 1 unit of the quote, zero landed
  losses, kill switch and limits exercised live.
- Stop conditions: any landed loss, any guard mismatch, landing rate under 2% after 500 bundles.

**Stage 5. Flash loans.** Only after Stage 4 passes. It removes the capital cap, and the flash fee is 0.1 bps.

## Funding

Rent figures are exact for today's sizes: 0.00203928 SOL per token account, 0.0583 SOL per full 256-address
table, 6,960 lamports per byte for program data. SOL was 84.8 USDC at discovery time.

| Item | SOL | Comes back? |
| --- | --- | --- |
| Executor program data (29,832 bytes; up to 2x if the CLI pads it) | 0.21 to 0.42 | Yes, on close |
| Deploy buffer and transaction fees | 0.02 | Buffer yes, fees no |
| Lookup tables for 250 armed pools (about 1,450 keys, 6 tables) | 0.35 | Yes, after deactivation |
| Token accounts for 150 intermediate mints | 0.31 | Yes, on close |
| Trading capital in wSOL | 0.50 | Yes |
| SOL float for tips and fees (`min_sol_float` 0.3 plus headroom) | 0.50 | Unspent part |
| **Total to start** | **1.9 to 2.1** | About 1.9 recoverable |

Split it across two keys: the deployer and upgrade authority holds the program rent (about 0.45 SOL); the hot
wallet holds the rest (about 1.65 SOL). The hot wallet is the only balance the executor can touch.

What 0.5 SOL of trading capital buys: it covers 34 of the 40 passing SOL routes but only 0.004 of their
0.013 SOL net, because the profit sits in the largest trades. 2 SOL covers 37 routes and 0.007 SOL. Capital
is not the constraint worth paying for; Stage 5 removes it.

**Recommended: fund 2.5 SOL** (about 210 USDC), which is the table plus a 0.4 SOL margin for table growth and
Token-2022 accounts that cost more than the standard size.

Money actually at risk in the test: transaction fees on landed bundles, capped at 0.02 SOL a day, plus the
hot wallet's balance if the executor has a bug the audit missed. Tips are paid only by bundles that land, and
the guard requires the route's output to cover them.

Recurring cost, separate from the wallet: the Helius plan. The stream at 8,000 pools measured about 5.4 GB an
hour, roughly 78 M credits a month at 20 credits per MB.

## Open questions for the owner

1. Jito's public block engine limits unauthenticated senders (about 1 request a second per region, to be verified). Two regions
   are configured. Is that enough, or apply for a higher limit before Stage 4?
2. Who audits the executor: owner alone, or a second reviewer?
3. Telegram `/stop`: needed for Stage 4, or is the KILL file over ssh enough for a supervised test?
