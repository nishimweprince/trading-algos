# Live readiness plan and funding (10 November 2026)

Goal: a small live test that measures the one thing dry run cannot, the Jito landing rate, with losses bounded
by design. Stages 0 and 1 are done and running in dry run. Stages 2 to 4 are the owner's.

## Where the bot stands

Measured on the Frankfurt VM, 8,000 pools tracked.

| Measure | Value |
| --- | --- |
| Dry-run bundles simulated, 13:01 to 16:51 UTC (3.8 h) | 244 |
| Fresh (simulated within 20 slots) and passing the guard | 48, net +0.0194 SOL |
| Trade size on passing SOL routes | p50 0.054 SOL, p90 1.30 SOL, max 2.35 SOL |
| Tip per passing bundle | p50 0.00008 SOL, max 0.0029 SOL |
| Kamino flash fee | 0.1 bps of the amount borrowed |
| 3-hop share of passing routes | 36 of 48 |
| Detect to signed, after Stage 0 | 7.6 ms p50, 8.8 ms p99 (was 73 ms / 100 ms) |
| Engine thread CPU, after Stage 0 | about 13% (was 100% to 120%) |

The measurable edge is about 0.005 SOL an hour before competition. The live test is an experiment, not income.

## Stage 0: trustworthy measurements (done)

| Fix | Effect |
| --- | --- |
| The DLMM clock account changes every slot and woke the engine for all 1,106 DLMM pools | Cycle evaluations 345k/s to 10k/s |
| One clock read per pool update, not per cycle (the VM's clock source is `xen`, so each read is a syscall) | About 40% of engine samples removed |
| Tick, bin and bitmap addresses derived once at start, not on every update | About 36% of engine samples removed |
| Settled pool/vault spreads count as quotable | Spread-stale skips 90% to about 12% of updates; RPC re-reads (5 a second) stopped |
| Frames read in bounded passes; each changed pool priced once per pass | The loop no longer starves shards or the metrics tick |

Open: the owner can switch the VM's clock source to `tsc` (a system setting), which makes every remaining
clock read cheap.

## Stage 1: send path behind the refusal (done)

Everything here runs in dry run or is unreachable until `dry_run = false`, which `live.cpp` still refuses.

| Piece | State |
| --- | --- |
| Armed set (`mode.armed_path`) | Loaded on the VM: 72 pools. Dry run counts routes outside it; live skips them as `not_armed`. |
| `live-plan` tool | Ranks pools by guard-passing fresh dry-run routes, writes the armed set, prints the rent. Read-only. |
| Jito sender | `Submitter` gets a `JitoClient` only when `dry_run = false`. |
| Landing watcher | Polls bundle statuses on its own thread; verdicts feed the tip controller and the reject pause. Built only when live. |
| Wallet state | SOL and base-token balances from the wallet stream reach the float check and own-capital sizing when live; gauges always. |
| Tip accounts | `getTipAccounts` hourly; only addresses in the built-in list are accepted. 8 loaded on the VM. |
| Kill switch | KILL file flips `arb_kill_switch` on the VM; the block is unit-tested. No candidate arrived during the VM test window. |
| Bug found | Every Jito request carried `Content-Type` twice and got HTTP 400. Fixed; `sendBundle` would have failed the same way. |

Not done, and why:
- **Executor-transaction simulation.** It needs the program on chain (Stage 2). Until then simulations run the
  swap legs directly.
- **Lookup-table and token-account creation.** These are mainnet transactions from the owner's wallet (Stage 3).
  `live-plan` prints what is needed; a tool that builds the unsigned transactions is the next piece of work.
- **Sweep job** (unwrap profit to the SOL float, excess to the cold wallet) and Telegram `/stop`.

## Why the live universe is a subset

Tracking 8,000 pools is free. Trading them is not: every pool needs lookup-table entries and every intermediate
mint needs a token account, both rent-funded. For all 8,000 pools that is about 23 SOL (179 tables, 6,167
token accounts).

`live-plan` on the last 24 h of dry run:

| | |
| --- | --- |
| Pools on a guard-passing fresh route | 72 |
| Lookup tables | 759 keys in 3 tables, 0.173 SOL |
| Token accounts | 23 intermediate mints, at least 0.047 SOL |
| Rent to lock | 0.22 SOL |

Re-run it before Stage 3; the set grows as dry run sees more routes. The cap is 250 pools.

## Flash loans first

The earlier plan started on own capital. There is no hard reason to:
- A flash loan is repaid inside the same transaction or the transaction reverts, so borrowed money cannot be lost.
- Flash mode keeps less of the owner's money in the hot wallet (no trading capital).
- The fee is 0.1 bps, and 0.5 SOL of own capital reaches only about 30% of the measured profit.

What flash needs first: a Kamino borrow and repay around the executor on a validator (never run), and a hard
size cap, because the config default allows 500 SOL a trade. So Stage 2 includes the Kamino round trip, and
Stage 4 starts on flash with `max_trade_lamports` at 2 SOL. Own capital stays as the fallback mode.

## Remaining stages (owner)

**Stage 2. Executor on chain (about 1 day)**
- Set `OWNER` in `program/executor/src/processor.rs` to the hot wallet's pubkey (it is a placeholder) and audit
  the 180 lines.
- Validator runs for Raydium v4 and Whirlpool (no run on record), the Kamino borrow/repay round trip, and a
  test that the tip stays inside `min_out` (the guard measures only the base token account; the tip is SOL).
- Deploy with a separate upgrade-authority key. Nothing trades yet.
- Exit: executor simulations match quotes to the unit on at least 200 routes; zero guard false passes.

**Stage 3. Fund and arm (about half a day)**
- Create the hot wallet, fund it, re-run `live-plan`, create the tables and token accounts.
- Uncomment the Stage 3 limits at the end of `deploy/bot.vm.toml`.
- KILL file tested once more with a candidate in flight.

**Stage 4. Live (owner flips the switch)**
- Remove the refusal in a reviewed commit and set `dry_run = false`.
- Exit (spec phase 4): at least 20 landed bundles, realized output within 1 unit of the quote, zero landed
  losses, kill switch and limits exercised live.
- Stop conditions: any landed loss, any guard mismatch, landing rate under 2% after 500 bundles.

## Funding

Rent: 6,960 lamports per byte (plus 128 bytes of overhead). SOL was 84.8 USDC at discovery time.

| Item | SOL | Comes back? |
| --- | --- | --- |
| Executor program data (29,832 bytes; up to 2x if the CLI pads it) | 0.21 to 0.42 | Yes, on close |
| Deploy buffer and transaction fees | 0.02 | Buffer yes, fees no |
| Lookup tables and token accounts for today's 72 armed pools | 0.22 | Yes |
| Room for the armed set to grow to 250 pools | 0.45 | Yes |
| SOL float for tips and fees (`min_sol_float` 0.3 plus headroom) | 0.50 | Unspent part |
| **Total on flash loans** | **1.4 to 1.6** | About 1.4 recoverable |
| Optional: own-capital fallback, wrapped SOL | 0.50 | Yes |

**Recommended: fund 2.0 SOL** (about 170 USDC): the flash total plus the fallback capital, or margin for
Token-2022 accounts that cost more than the standard size. The minimum to start today is about 1.2 SOL.

Split it across two keys: the deployer and upgrade authority holds the program rent (about 0.45 SOL); the hot
wallet holds the rest. The hot wallet is the only balance the executor can touch.

Money at risk: transaction fees on landed bundles, capped at 0.02 SOL a day, plus the hot wallet's balance if
the executor has a bug the audit missed. Tips are paid only by bundles that land, and the guard requires the
route's output to cover them.

Recurring, separate from the wallet: the Helius plan. The stream at 8,000 pools measured about 5.4 GB an hour,
roughly 78 M credits a month at 20 credits per MB.

## Open questions for the owner

1. Jito's public block engine limits unauthenticated senders (about 1 request a second per region, to be
   verified). Two regions are configured. Enough for the test, or apply for a higher limit first?
2. Who audits the executor: owner alone, or a second reviewer?
3. Telegram `/stop`: needed for Stage 4, or is the KILL file over ssh enough for a supervised test?
