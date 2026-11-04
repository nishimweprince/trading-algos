# Solana Atomic Arbitrage Bot: C++ Implementation Spec (Jito + Helius + Flash Loans)

Nov 4, 2026 · @Nishimwe Prince

## 1. Overview and rules for the coding agent

The bot finds price gaps between Solana DEX pools, then borrows with a flash loan, swaps around a cycle, repays and keeps the profit, all in one transaction. That transaction is submitted as a Jito bundle with a tip, and it reverts on-chain if the profit check fails, so a losing trade can never land.

**Goals**

- Cyclic arbitrage across Solana DEX pools (2-hop and 3-hop cycles), triggered by on-chain pool state changes.
- Near-zero working capital: every trade is funded by a same-transaction flash loan; the wallet only holds SOL for fees and tips.
- Off-chain engine in **C++20**, low latency, deterministic integer math.
- On-chain **executor program in Rust**. Solana programs compile to sBPF; there is no supported C++ toolchain for on-chain code, so this part must be Rust (Anchor or native).
- Every submitted transaction is guarded by an on-chain minimum-profit check.

**Non-goals (do not build)**

- Sandwiching or front-running user transactions. Jito's block engine rejects bundles matching sandwich patterns, and it is out of scope.
- CEX-DEX or any non-atomic strategy that carries inventory risk.
- A UI. Output is logs, metrics and a Telegram alert channel.

**How the agent should use this spec**

1. Build in the phase order of section 13. Do not start a phase until the previous phase's acceptance tests pass.
2. Treat every protocol detail marked **VERIFY** as unconfirmed: read the official IDL, SDK or docs for that protocol before coding it, and pin the program ID and version in config.
3. All on-chain amounts are `uint64_t` raw units. Use `unsigned __int128` for intermediate products. Never use floating point in the quote or sizing path that decides what is sent.
4. Every DEX quote function must have a parity test against the protocol's official SDK on recorded pool snapshots (section 11). A quoter without parity tests is not done.
5. Never put a private key in code, logs or the repo. Keys load from a file path in config with `0600` permissions.
6. Default mode is `dry_run = true`: the bot builds and simulates bundles but does not send them. Going live is an explicit config change.

**Conventions**

- C++20, CMake, `clang-format` (Google style, 100 cols), `-Wall -Wextra -Werror`.
- Namespaces: `arb::feed`, `arb::state`, `arb::dex`, `arb::engine`, `arb::flash`, `arb::tx`, `arb::submit`, `arb::risk`, `arb::obs`.
- Errors via `std::expected<T, Error>` (C++23 library or `tl::expected`); no exceptions on the hot path.
- Pubkeys as a 32-byte `std::array<uint8_t,32>` type with base58 encode/decode at the edges only.

## 2. System architecture

A single Frankfurt host runs the whole hot path in C++; the only Rust is the on-chain executor program that the transaction calls.

&#91;embedded content: hot path from pool update to landed bundle\]

Pool updates flow left to right through the engine, then back through the risk guard and builder to the submitter, which hands one transaction to Jito; the leader executes it atomically.

**Components (one module each, see section 12 for paths)**

1. **Feed** (`arb::feed`): LaserStream gRPC clients, subscription manager, slot clock.
2. **State store** (`arb::state`): account decoders and per-pool snapshots.
3. **Quoters** (`arb::dex`): exact swap math per DEX, called by the engine.
4. **Decision engine** (`arb::engine`): cycle index, prefilter, sizer, cost model, tip policy, dispatcher.
5. **Flash module** (`arb::flash`): Kamino and marginfi instruction builders and live reserve data.
6. **Risk** (`arb::risk`): token safety, limits, kill switch, pre-sign allowlist check.
7. **Tx builder** (`arb::tx`): instruction builders, v0 message compiler, ALT manager, signer.
8. **Submitter and tracker** (`arb::submit`): Jito and Sender clients, bundle status tracking. Outcomes feed back into the engine's landing-rate and tip models.
9. **Background tools**: discovery, ALT creation, blockhash and tip-floor refresh, sweeps.
10. **Executor program** (Rust, on chain): runs the swaps by CPI and enforces `min_out`.

**Language split.** Off-chain is C++20 because the hot path is latency-bound and you want full control of memory and threads. On-chain must be Rust (sBPF target); keep it small, stateless and audited by tests.

## 3. Infrastructure: Helius, Jito and hosting

Run the hot path on one bare-metal or cloud host in Frankfurt (fallback Amsterdam), streaming pool state from Helius LaserStream gRPC and submitting bundles to the nearest Jito block engine. Develop locally in Kigali against devnet and recorded data only.

**What the Helius Business plan gives this bot** (as of Nov 2026, [Helius LaserStream blog](https://www.helius.dev/blog/laserstream-websockets), [Helius pricing](https://helius.dev/pricing), [Helius agent docs](https://www.helius.dev/docs/agents/overview))

| Product | Use in this bot | Plan detail |
| --- | --- | --- |
| LaserStream gRPC (mainnet) | Primary feed: account updates for every tracked pool, vault and flash-loan reserve | Mainnet on Business since 7 Apr 2026, up to 10 concurrent gRPC connections; Yellowstone-compatible API |
| Standard RPC | Pool discovery, `getMultipleAccounts` resync, `getLatestBlockhash`, `simulateTransaction`, ALT management | 200 RPC req/s, 100M credits/month |
| Sender (`https://sender.helius-rpc.com/fast`) | Optional second submit path for single transactions; routes to validators and Jito in parallel | Included on all plans, 7 regional endpoints, tip required (min tip quoted as 0.000005 SOL on the pricing page and 0.001 SOL by a third party: **VERIFY**) |
| Priority Fee API | Input to compute-unit price for the Sender path | Standard RPC credits |

**Bandwidth budget.** LaserStream bills by data volume (20 credits per MB per the blog above). 100M credits covers about 5 TB/month total. Subscribe to specific pool and vault accounts only, never to whole programs, and log bytes received per subscription.

**Jito block engine.** Regional endpoints include `frankfurt`, `amsterdam`, `ny`, `tokyo` and `slc` under `https://<region>.mainnet.block-engine.jito.wtf` ([Chainstack Jito guide](https://chainstack.com/jito-explained-bundles-tips-mev-solana/)). Key facts the engine relies on:

- Bundles hold up to 5 transactions, execute in order, and are all-or-nothing.
- Minimum tip 1,000 lamports per bundle; tip accounts come from `getTipAccounts`, refreshed at startup and hourly.
- The block engine runs auctions in 50 ms ticks; latency from bot to block engine decides competitiveness.
- Default unauthenticated rate limits apply per IP and region (**VERIFY** current limits; request a higher limit from Jito once landing data exists).

**Hosting**

| Stage | Host | Notes |
| --- | --- | --- |
| Dev and replay | Local machine (Kigali) | Devnet, recorded LaserStream captures, no mainnet sends |
| Prototype live | Cloud VM in Frankfurt, 8+ vCPU, 32 GB RAM, NVMe | Measure RTT to Helius and every Jito region before choosing |
| Production | Bare metal in Frankfurt or Amsterdam | Pin hot-path threads to isolated cores; second node in New York later |

**Keys and secrets**

- `hot_wallet.json`: pays fees and tips, signs transactions. Holds only a small SOL float (start with 0.5 to 1 SOL). Never holds strategy capital.
- `jito_auth.json` (optional): separate keypair for Jito authentication if you obtain a whitelisted UUID.
- Helius API key in an environment variable, not in config files committed to git.
- Profits sweep from the hot wallet to a cold address on a schedule (section 10).

## 4. Market data layer

The bot keeps an in-memory, slot-stamped copy of every tracked pool's state, updated by LaserStream account pushes, and never calls RPC on the hot path.

**Supported DEXes (phase order).** Start with two, add one at a time after each passes parity tests. Program IDs below are from memory: **VERIFY each against the protocol's official docs** before use.

| Phase | DEX | Model | Program ID (VERIFY) | Accounts to subscribe |
| --- | --- | --- | --- | --- |
| 1 | Raydium AMM v4 | Constant product | `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8` | Pool state, base vault, quote vault (OpenBook market accounts if the swap still requires them) |
| 1 | Orca Whirlpool | Concentrated liquidity | `whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc` | Whirlpool, both vaults, tick arrays around the current tick (current plus 2 each side) |
| 2 | Raydium CPMM | Constant product (Token-2022 aware) | `CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C` | Pool state, vaults, AMM config |
| 2 | Meteora DLMM | Discrete bins | `LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo` | LB pair, bin arrays around the active bin, vaults |
| 3 | Raydium CLMM | Concentrated liquidity | `CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK` | Pool state, vaults, tick arrays, bitmap extension |
| 3 | PumpSwap AMM | Constant product | `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` | Pool, vaults, global config (fees) |

**Pool discovery (off the hot path, every 10 minutes)**

1. For each DEX, list pool accounts with `getProgramAccounts` plus `dataSize` and `memcmp` filters on the mint fields, restricted to pools containing a base asset from config (`SOL`, `USDC`, `USDT` at first).
2. Build the token graph: nodes are mints, edges are pools. Keep only mints that appear in at least 2 pools, since a mint in one pool cannot form a cycle.
3. Score each pool by TVL (vault balances valued in SOL) and drop pools under `min_pool_tvl_sol` (default 50 SOL).
4. Cap the tracked set at `max_tracked_pools` (default 1,500). Diff against the current set and add or remove LaserStream subscriptions.
5. Run every new mint through the token safety check (section 10) before any pool containing it is tracked.

**Subscriptions.** One LaserStream gRPC `SubscribeRequest` per shard of about 300 accounts, using `accounts` filters by pubkey at `processed` commitment. Use 3 to 4 of the 10 allowed connections for pools, 1 for the wallet and flash-loan reserves, and keep the rest spare for reconnects. Also subscribe to `slots` to keep a current slot clock.

**Decoding.** Each DEX has a decoder `std::expected<PoolState, Error> decode(const AccountUpdate&)` built from the protocol's published IDL or Rust struct layout. Rules:

- Verify the 8-byte Anchor discriminator (where applicable) and the exact account length before decoding.
- Decode vault balances from SPL Token and Token-2022 account layouts directly. Read Token-2022 extensions: a transfer fee changes the math, and transfer hooks or a permanent delegate mark the mint unsafe.
- A pool is quotable only when the pool account and all its vaults and tick or bin arrays have been seen at a slot no older than `max_state_age_slots` (default 2) relative to the newest update.

**State cache.** A single-writer `StateStore` owned by the feed thread. Readers get immutable snapshots through an RCU-style pointer swap per pool (`std::atomic<std::shared_ptr<const PoolState>>`). Each `PoolState` carries `slot`, `write_version` and a monotonic `seq`. The store pushes the pool id onto a lock-free queue for the decision engine on every change.

**Resync and gaps.** On reconnect, or when a pool's slot lags the slot clock by more than 150 slots, fetch it with `getMultipleAccounts` (batches of 100) and mark it quotable only after a fresh push arrives. Use LaserStream's replay from the last seen slot when reconnecting, if available on your plan (**VERIFY**).

## 5. Pricing engine (exact quoters)

Each DEX gets a quoter that reproduces the on-chain program's output to the last unit, including fee rounding; an off-by-one here becomes a reverted bundle or a wrong profit estimate.

**Interface**

```cpp
namespace arb::dex {
struct Quote {
  uint64_t amount_in;      // actually consumed
  uint64_t amount_out;     // exactly what the program will transfer out
  uint64_t fee_paid;       // in input-token units
  uint32_t est_compute_units;
  bool     crosses_boundary;  // true if a tick/bin array outside the cache would be needed
};
class IQuoter {
 public:
  virtual ~IQuoter() = default;
  virtual std::expected<Quote, Error> quote_exact_in(const PoolState&, bool a_to_b, uint64_t amount_in) const = 0;
  virtual DexKind kind() const = 0;
};
}  // namespace arb::dex
```

If `crosses_boundary` is true, the engine discards that size, since the transaction would need accounts the bot is not tracking.

**Constant product (Raydium AMM v4, CPMM, PumpSwap).** Fee is taken from the input; integer division floors.

```latex
\Delta x' = \left\lfloor \frac{\Delta x \cdot (D - f)}{D} \right\rfloor, \qquad \Delta y = \left\lfloor \frac{\Delta x' \cdot R_y}{R_x + \Delta x'} \right\rfloor
```

Here R are vault balances (minus any amounts the program excludes, such as accrued protocol or fund fees in CPMM: **VERIFY** per program), f/D is the fee rate as a fraction (for example 25/10000 for 0.25%). Some programs round the fee up instead of down; copy the exact expression from the program source. PumpSwap splits fees among LP, protocol and creator; sum them as the program does.

**Concentrated liquidity (Orca Whirlpool, Raydium CLMM).** Price is a Q64.64 fixed-point square root, `sqrt_price_x64`. Port the swap loop from the program source, step by step:

1. Find the next initialized tick in the swap direction from the cached tick arrays. If none is cached within range, set `crosses_boundary`.
2. Compute the maximum input that moves the price to that tick, with fee applied to the input of each step.
3. If the remaining input fits, compute the new sqrt price and output; otherwise move to the tick, apply its `liquidity_net`, and continue.
4. Round input up and output down at each step, exactly as the program's `swap_math` does.

The per-step amounts, for liquidity L between sqrt prices a < b:

```latex
\Delta\text{token}_A = L \cdot \frac{\sqrt{P_b} - \sqrt{P_a}}{\sqrt{P_a}\,\sqrt{P_b}}, \qquad \Delta\text{token}_B = L \cdot (\sqrt{P_b} - \sqrt{P_a})
```

Use `unsigned __int128` for all products, and a 256-bit helper (for example a small `u256` struct) where the program uses `U256`. Do not approximate with doubles.

**Discrete bins (Meteora DLMM).** Each bin has a fixed price and acts as constant-sum inside the bin:

```latex
P_{\text{bin}} = \left(1 + \frac{\text{bin\_step}}{10000}\right)^{\text{bin\_id}}
```

The program stores this as a Q64.64 value and computes it with its own power function; port that function exactly rather than using `pow`. The fee is a base fee plus a variable fee driven by a volatility accumulator that changes as bins are crossed during the swap. Port the fee update logic from the program, because the fee for the second bin depends on the first.

**Token-2022 transfer fees.** If either mint has the transfer-fee extension, subtract the fee on every transfer into and out of the pool, using the epoch-appropriate fee config. Mints with transfer hooks are excluded by the safety filter, so the quoter can reject them.

**Performance target.** Constant product under 100 ns per quote; CLMM and DLMM under 2 µs per quote for swaps crossing up to 5 ticks or bins, single-threaded on the production host.

**Parity requirement.** For each quoter, record at least 1,000 pool snapshots from mainnet, quote 20 sizes each, and compare to the official SDK (Raydium SDK, Orca Whirlpools SDK, Meteora DLMM SDK) run in a Node or Rust test harness. Target: 100% exact match on `amount_out`. Any mismatch blocks that DEX from live trading.

## 6. Decision engine

On every pool update, the engine re-prices only the cycles that touch that pool, sizes each one optimally, subtracts every cost, and sends the single best bundle whose expected value clears the threshold.

### 6.1 Route graph and cycle index

- **Base tokens** are tokens you can flash-borrow: start with SOL (wSOL) and USDC. Every cycle starts and ends in a base token.
- **2-hop cycles:** base → X on pool P1, X → base on pool P2, where P1 and P2 are different pools for the same pair.
- **3-hop cycles:** base → X → Y → base across three pools.
- Precompute all cycles at discovery time (section 4) up to `max_hops` (default 3). Store a `cycle_index: pool_id → vector<cycle_id>`. Rebuild on every discovery run.
- Cap cycles per pool at `max_cycles_per_pool` (default 64), keeping the ones through the deepest pools, so one busy pool cannot stall the engine.

### 6.2 Event loop (per update)

```cpp
void Engine::on_pool_update(PoolId p, Slot slot) {
  for (CycleId c : cycle_index_[p]) {
    if (!all_legs_fresh(c, slot)) continue;           // section 4 staleness rule
    if (cooldown_.active(c)) continue;
    if (spot_edge(c) < cfg_.min_spot_edge) continue;  // cheap prefilter, 6.3
    auto opp = size_and_price(c);                     // 6.4 and 6.5
    if (!opp) continue;
    if (opp->ev_lamports < cfg_.min_ev_lamports) continue;
    candidates_.push(*opp);
  }
  dispatch_best(slot);                                // 6.7
}
```

The loop runs on one pinned thread. Target: under 200 µs from update received to bundle handed to the submitter for 3-hop CLMM cycles.

### 6.3 Prefilter (spot edge)

For each leg, compute the marginal output per unit of input at size zero, after the pool's fee. The cycle is worth sizing only if the product exceeds one by a margin:

```latex
\text{spot\_edge} = \prod_{i=1}^{n} r_i \cdot (1 - f_i) - 1
```

Compute it in fixed point from cached sqrt prices or reserves. `min_spot_edge` defaults to 0.0005 (5 bps). This rejects over 99% of cycles without running a full quote.

### 6.4 Optimal sizing

Profit as a function of input size x is concave for these pool types, so search for its maximum:

```latex
\pi(x) = \text{out}_n(\ldots \text{out}_1(x)) - x - \text{flash\_fee}(x)
```

1. Upper bound `x_max` = the smallest of: flash reserve available liquidity, `max_input_fraction` (default 20%) of the shallowest leg's input-side depth, and `max_trade_lamports` from config.
2. For 2-hop constant-product cycles, seed with the closed-form optimum; otherwise seed at `x_max / 4`.
3. Run golden-section search on integers over `[min_trade, x_max]`, at most 24 evaluations, using the exact quoters.
4. Reject the result if any leg reports `crosses_boundary` at the chosen size; retry once at 70% of that size.

### 6.5 Cost model and net profit

All values converted to lamports at the current SOL price from the deepest SOL/USDC pool.

| Cost | Source | Notes |
| --- | --- | --- |
| Flash loan fee | Reserve config read from chain (section 7) | Kamino: `flash_loan_fee_sf` scaled fraction of amount |
| Jito tip | Tip policy, 6.6 | Paid only if the bundle lands, because the tip is inside the arb transaction |
| Base fee | 5,000 lamports per signature | One signature per transaction |
| Priority fee | CU price × CU limit | Usually zero or minimal on the bundle path; needed on the Sender path |
| ATA creation | Rent of a token account (about 0.002 SOL) | Pre-create all intermediate ATAs at startup so this is zero on the hot path |

```latex
\text{net} = \pi(x^*) - \text{tip} - \text{fees}, \qquad \text{EV} = p_{\text{land}} \cdot \text{net} - (1 - p_{\text{land}}) \cdot c_{\text{fail}}
```

On the Jito bundle path a failed bundle does not land, so the failure cost is close to zero. On the Sender path a failed transaction still pays base and priority fees, so it carries a real failure cost. Estimate `p_land` per route class from the bot's own history (section 11), starting at 0.1.

### 6.6 Tip policy

The tip is the bid in Jito's auction, so it decides who wins the same opportunity.

```latex
\text{tip} = \text{clamp}\big(\alpha_k \cdot \pi(x^*),\; \text{tip}_{\min},\; \text{tip}_{\max}\big)
```

- `alpha_k` is a per-route-class share of gross profit (class = number of hops plus DEX mix). Start at 0.5.
- After every 50 attempts in a class, adjust: if the landing rate is below `target_land_rate` (default 0.3), raise alpha by 0.05; if above, lower it by 0.05. Bound alpha to \[0.2, 0.95\].
- `tip_min` = max(1,000 lamports, current Jito tip floor at the 25th percentile). Read tip-floor percentiles from Jito's tip floor API (**VERIFY** endpoint) every 10 seconds.
- Never let `tip` push `net` below `min_net_lamports`.

### 6.7 Dispatch and conflicts

- Candidates in one engine pass go into a max-heap by EV.
- Pop the best. Skip any later candidate that shares a pool with one already sent in this slot, since they compete for the same liquidity.
- Send at most `max_bundles_per_slot` (default 3).
- After sending, put the cycle on cooldown for 2 slots to avoid re-sending a stale opportunity.
- Drop any candidate whose state is older than the current slot by more than `max_send_lag_slots` (default 1) by the time it reaches dispatch.

### 6.8 On-chain guard value

The executor program receives `min_out` and checks it after the last swap (section 8). Set it to the amount needed to repay the flash loan plus fee, plus the tip, plus `min_net_lamports`. If any pool moved before execution, the check fails and the whole transaction reverts, so the bundle cannot land at a loss.

### 6.9 Config parameters (defaults)

| Key | Default | Meaning |
| --- | --- | --- |
| `max_hops` | 3 | Longest cycle |
| `min_spot_edge` | 0.0005 | Prefilter threshold |
| `min_ev_lamports` | 20,000 | Smallest expected value worth sending |
| `min_net_lamports` | 10,000 | Floor profit enforced on chain |
| `max_trade_lamports` | 500 SOL equivalent | Cap per trade, flash-funded |
| `max_input_fraction` | 0.20 | Share of the shallowest leg's depth |
| `target_land_rate` | 0.30 | Tip controller target |
| `max_bundles_per_slot` | 3 | Dispatch cap |

## 7. Flash loans

Kamino Lend is the primary lender: the arb transaction wraps the swap instruction between a top-level `flash_borrow_reserve_liquidity` and a matching `flash_repay_reserve_liquidity`, so the wallet needs only SOL for fees and tips.

### 7.1 Providers

| Provider | Instructions | Fee | Setup | Role |
| --- | --- | --- | --- | --- |
| [Kamino Lend](https://www.mintlify.com/kamino-finance/klend/advanced/flash-loans) (program `KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD`) | `flash_borrow_reserve_liquidity(amount)`, `flash_repay_reserve_liquidity(amount, borrow_instruction_index)` | Per reserve, from `reserve.config.fees.flash_loan_fee_sf`; the value `u64::MAX` means flash loans are disabled for that reserve | None beyond ATAs | Primary |
| [marginfi v2](https://blog.asymmetric.re/threat-contained-marginfi-flash-loan-vulnerability) | `lending_account_start_flashloan(end_index)` … borrow, swaps, repay … `lending_account_end_flashloan` | **VERIFY** per bank config | One-time `marginfi_account` creation (rent) | Fallback when a Kamino reserve is short or disabled |
| Own capital | None | None | Hold the base token in the wallet | Phase 0 testing and tiny trades only |

### 7.2 Kamino rules the transaction must satisfy

From Kamino's documentation of the program's checks ([source](https://www.mintlify.com/kamino-finance/klend/advanced/flash-loans)):

- The repay instruction comes after the borrow in the same transaction and references the same reserve and accounts.
- The repay `liquidity_amount` equals the borrow amount; the fee is collected on top of it.
- `borrow_instruction_index` is the absolute index of the borrow instruction in the transaction, counting compute-budget instructions. Compute it when assembling, never hard-code it.
- The reserve must have enough available liquidity.
- Both instructions must be top-level, not called through CPI (**VERIFY** for Kamino; marginfi explicitly rejects CPI for start and end).

The fee is split between the reserve supply vault and a fee vault, plus a referrer share if a referrer is passed. Do not pass a referrer. Reproduce `calculate_flash_loan_fees` exactly in C++ from the program source, and read `flash_loan_fee_sf` from each reserve account through the LaserStream subscription so fee changes are picked up live.

### 7.3 Transaction layout

One versioned transaction, inside a one-transaction Jito bundle:

| Index | Instruction | Notes |
| --- | --- | --- |
| 0 | `ComputeBudget::SetComputeUnitLimit` | From the quoters' CU estimates plus 15% headroom |
| 1 | `ComputeBudget::SetComputeUnitPrice` | Omit or set low on the bundle path |
| 2 | Kamino `flash_borrow_reserve_liquidity(x)` | Destination: the bot's base-token ATA |
| 3 | Executor `execute_route(route, x, min_out)` | All swaps by CPI, then the profit check (section 8) |
| 4 | Kamino `flash_repay_reserve_liquidity(x, 2)` | Source: the same ATA; pulls x plus fee |
| 5 | `System::Transfer` tip to a Jito tip account | Inside the same transaction, so it is paid only when the arb succeeds |

If any instruction fails, nothing in the transaction applies and the bundle does not land.

### 7.4 Choosing the base token and reserve

- Keep a `FlashReserve` record per base token: reserve pubkey, liquidity mint, supply vault, fee vault, lending market and its authority PDA, token program, fee rate, available liquidity, last-updated slot.
- Available liquidity bounds `x_max` in section 6.4.
- For SOL-based cycles borrow wSOL; for USDC cycles borrow USDC. Do not add a conversion leg just to use a different lender.

### 7.5 Tip and profit currency

The tip is paid in native SOL from the hot wallet's float, while profit accrues in the base token. For USDC cycles, the guard in section 6.8 converts the tip to USDC at the engine's SOL price, so the USDC profit always covers the tip. A maintenance job swaps accumulated USDC profit to SOL when the float drops below `min_sol_float` (default 0.3 SOL).

### 7.6 Own-capital mode

A config switch `funding_mode: flash | own`. In `own` mode, steps 2 and 4 are omitted, and the engine caps `x_max` at the wallet balance. Use it for the first live tests with 0.1 to 0.5 SOL, so flash-loan wiring and swap wiring can be debugged separately.

## 8. On-chain executor program (Rust)

A small, stateless Rust program executes the swap legs by CPI, measures real balance changes, and fails the whole transaction unless the final output meets `min_out`.

**Why a custom program.** Calling each DEX as a separate top-level instruction cannot pass one leg's actual output into the next, and cannot enforce one profit check across all legs. A program also uses fewer accounts and compute units than an aggregator route.

**Framework.** Native `solana-program` (or Pinocchio) with zero-copy account access. Anchor is acceptable for phase 1 but costs extra compute units; plan to port before scaling. No `msg!` logging in release builds.

**Instruction: `execute_route`**

```rust
// Instruction data, little-endian, packed
struct ExecuteRouteArgs {
    amount_in: u64,      // flash-borrowed amount x
    min_out: u64,        // x + flash_fee + tip_in_base + min_net
    num_legs: u8,        // 2 or 3
    legs: [LegSpec; 3],  // only num_legs used
}
struct LegSpec {
    dex: u8,             // enum: RaydiumV4, Whirlpool, Cpmm, Dlmm, Clmm, PumpSwap
    a_to_b: u8,          // direction
    acct_start: u8,      // offset into remaining accounts
    acct_len: u8,        // number of accounts for this leg
}
```

**Accounts.** Fixed: signer (hot wallet), base-token ATA, then one ATA per intermediate token, then each leg's DEX accounts in order (`acct_start` .. `acct_start + acct_len`). The C++ builder must produce the exact account order each DEX's swap instruction expects; take it from each program's IDL.

**Execution logic**

1. Require the signer to equal a hard-coded owner pubkey, so nobody else can route through your program.
2. Read the base ATA balance `pre`. It already includes the borrowed x.
3. For each leg: read the input ATA balance, CPI the DEX swap with the full input (for leg 1 that is `amount_in`; later legs use the measured output of the previous leg) and a per-leg minimum of 0 or 1. Measure the output ATA's balance delta and use it as the next leg's input.
4. Read the base ATA balance `post`. Compute `out = post − (pre − amount_in)`.
5. If `out < min_out`, return a custom error. The runtime then reverts every instruction in the transaction, including the flash borrow.

Per-leg minimums are left at 0 on purpose: the single end-of-route check is what protects the trade, and per-leg checks only add compute units.

**Compute budget.** A transaction may request at most 1.4M compute units. Measure each DEX's swap CU on devnet or a local validator, store the figures in config, and let the quoters report them. Reject 3-hop routes whose estimate plus Kamino plus overhead exceeds 1.2M.

**Address Lookup Tables (ALTs).** A Solana transaction is limited to 1,232 bytes, and a 3-hop CLMM route can need 40 or more accounts, so lookup tables are required.

- One shared ALT holds programs, mints, token programs, the Kamino reserve accounts and the bot's ATAs.
- One ALT per active pool holds that pool's accounts, including the tick or bin arrays near the current price.
- Create and extend ALTs from the discovery job. A newly extended address is usable only after the table's activation slot passes, so keep new pools in a `pending_alt` state for 2 slots before routing through them.
- Track tick or bin array changes: when the price moves into an array that is not in the pool's ALT, extend the ALT in the background.
- Whether Jito tip accounts may come from an ALT: **VERIFY** in Jito docs. Default to writing the tip account as a static key.

**Testing.** Run the program on `solana-test-validator` with cloned mainnet accounts (`--clone` for each pool, vault, tick array and Kamino reserve), then on mainnet with tiny own-capital trades before enabling flash loans.

**Deployment.** Deploy to mainnet once phase 3 tests pass. Keep the upgrade authority on a separate key from the hot wallet.

## 9. Transaction building and submission

The submitter turns an opportunity into a signed v0 transaction in under 50 µs and sends it as a one-transaction Jito bundle to the nearest block engine regions at once.

**Building (C++, no Solana SDK dependency)**

1. Keep a recent blockhash cache refreshed every 2 seconds from Helius RPC (`getLatestBlockhash`, `processed`). A blockhash is valid for about 150 slots; refuse to build if the cached one is older than 60 slots.
2. Assemble instructions in the order of section 7.3. Compute `borrow_instruction_index` from the final position.
3. Compile a v0 `MessageV0`: deduplicate account keys, order them signer-writable, signer-readonly, non-signer-writable, non-signer-readonly; move every key that exists in an attached ALT into the lookup section, except signers and the tip account.
4. Serialize with Solana's compact-u16 length encoding. Check the size is at most 1,232 bytes; if not, drop the route and log `tx_too_large` with the route class.
5. Sign the message bytes with Ed25519 (libsodium `crypto_sign_detached`). The 64-byte signature goes before the message.
6. Pick a Jito tip account at random from the cached `getTipAccounts` list for each transaction.

Unit-test the serializer against transactions produced by the official Rust or JS SDK for the same inputs: byte-for-byte equality is required.

**Submission paths**

| Path | Method | When | Failure cost |
| --- | --- | --- | --- |
| Jito bundle (default) | JSON-RPC `sendBundle` with `[base64_tx]` and `{"encoding":"base64"}` to `https://<region>.mainnet.block-engine.jito.wtf/api/v1/bundles` | Every arb | Near zero: a failing bundle does not land |
| Helius Sender (optional) | `sendTransaction` to `https://sender.helius-rpc.com/fast` with `skipPreflight`, priority fee and a Jito tip | Only if bundle landing is poor and the route's EV covers the failure cost | Base fee plus priority fee on every failed landing |

- Fan out each bundle to `frankfurt` and `amsterdam` in parallel (later `ny` too), over persistent HTTP/2 connections with keep-alive, using a dedicated I/O thread and a pre-warmed connection pool (for example `libcurl` multi or `nghttp2`).
- Do not retry a bundle after the slot passes. A stale arb is a new decision, not a retry.
- Respect Jito's per-IP rate limits; when you receive HTTP 429, back off that region for 1 second and record it.

**Landing confirmation**

- Track each `bundle_id` and transaction signature. Poll `getInflightBundleStatuses` and `getBundleStatuses` every 500 ms for up to 30 seconds (**VERIFY** method names and limits in Jito's API docs).
- Independently subscribe to the hot wallet's account through LaserStream: a balance change confirms a landing faster than polling.
- Record outcome per attempt: `landed`, `not_landed`, `rejected` (with Jito's error), `simulation_failed`. These feed the landing-probability and tip-controller models in section 6.

**Optional pre-send simulation.** In phases 2 and 3, call `simulateTransaction` (Helius RPC, `replaceRecentBlockhash: true`, `sigVerify: false`) before sending, and log any mismatch between simulated and quoted output. Disable it in phase 4 except for 1 in 20 sampled attempts, because it adds a network round trip.

## 10. Risk management and safety

The on-chain guard stops losing trades, but it cannot stop drained fees, poisoned tokens or a leaked key; these controls cover the rest.

**Token safety filter (run before tracking any mint)**

- Reject Token-2022 mints with a transfer hook, permanent delegate, non-transferable flag, default-frozen account state, or confidential transfers.
- Allow the transfer-fee extension only if the quoter handles it, and cap the fee at 1%.
- Reject mints whose freeze authority is set, unless the mint is on an allowlist (USDC and USDT have freeze authorities and must be allowlisted).
- Reject pools where either vault is not owned by the expected token program, or where the pool account's owner is not the expected DEX program.
- Keep a `denylist.json` of mints and pools; any reverted simulation with an unexpected token error adds the mint for 24 hours.

**Limits and kill switches**

| Control | Default | Action |
| --- | --- | --- |
| `max_daily_fee_spend_sol` | 0.2 SOL | Stop sending when base, priority and Sender fees spent today exceed this |
| `max_sol_float` | 2 SOL | Sweep excess to the cold address |
| `min_sol_float` | 0.3 SOL | Stop sending and alert if below |
| `max_consecutive_rejects` | 200 | Pause 5 minutes and alert; often a bug in account ordering |
| `max_feed_lag_ms` | 800 | Pause sending while the newest LaserStream update is older than this |
| `quote_mismatch_rate` | 2% of simulated sends | Disable the offending DEX quoter and alert |
| Manual kill | File `KILL` in the run directory, or Telegram `/stop` | Stop sending immediately; keep feeds running |

**Key security**

- The hot wallet signs only transactions the bot builds. Before signing, the builder checks that every instruction's program is in an allowlist (ComputeBudget, System, Kamino, your executor, SPL Token, Token-2022, Associated Token) and that the only System transfer goes to a Jito tip account with an amount at most `tip_max`.
- Run the bot as a dedicated Unix user; key files at `0600`; no SSH password login; firewall allows outbound only plus your admin IP.
- Profits sweep daily to a cold wallet whose key never touches the server.

**Operational risks to plan for**

- Pool program upgrades change account layouts. Validate discriminator and length on every decode, and alert on any decode failure spike.
- Kamino can disable flash loans on a reserve or change the fee; the live reserve subscription catches both, and the engine switches to the marginfi fallback or skips the cycle.
- Competitors may copy your executor's call pattern; this does not put funds at risk because of the signer check, but it can erode edge.

## 11. Observability, replay and testing

Every decision the engine makes is logged with enough data to replay it, so you can measure the edge offline before risking SOL.

**Metrics (Prometheus exposition on `localhost:9100`)**

- Feed: updates per second, bytes per second per connection, update age (ms) at receipt, reconnects.
- Engine: cycles evaluated per update, prefilter pass rate, sizing time (µs histogram), candidates per slot.
- Submit: bundles sent per region, HTTP status counts, send latency (µs).
- Outcomes: landed, not landed, rejected per route class; landing rate; tip paid; net profit in lamports; tip share of gross.
- Money: hot wallet SOL, base-token balances, daily fee spend.

**Decision log.** One binary record (or Parquet row) per candidate: slot, cycle id, pool states' slot and seq, size, quoted outputs per leg, costs, tip, EV, action (sent or skipped with reason), and later the outcome. Rotate hourly; keep 14 days.

**Recorder and replay**

1. A `recorder` mode writes raw LaserStream account updates with receive timestamps to disk, without trading.
2. A `replay` mode feeds recorded updates through the same state store and engine at original or accelerated speed, writing a decision log.
3. Compare replayed opportunities against what actually landed on chain in those slots (from Jito bundle data or block data). This tells you how many of your detections a competitor took, and at what tip.

Record for at least 3 days before writing any send code beyond phase 2.

**Test layers**

| Layer | Tool | Pass bar |
| --- | --- | --- |
| Quoter parity | GoogleTest plus SDK harness on recorded snapshots | 100% exact `amount_out` |
| Decoders | Fixtures of real account bytes per DEX | All fields match SDK decode |
| Tx serializer | Golden transactions from the Rust or JS SDK | Byte-identical |
| Executor program | `solana-test-validator` with cloned mainnet accounts | Profitable route succeeds; tampered route reverts with the custom error |
| End to end | Devnet, then mainnet own-capital at 0.1 SOL | Landed trades' realized output within 1 unit of quote |
| Engine performance | Google Benchmark | Section 5 and 6.2 latency targets met |

**Alerts (Telegram bot)**: kill switch triggered, float low, feed lag, decode failure spike, quoter disabled, daily P&L summary.

## 12. Project layout, libraries and config

One CMake monorepo with a C++ engine, a Rust executor program and a small Node or Rust harness used only for parity tests.

**Repository layout**

```text
sol-arb/
  CMakeLists.txt  CMakePresets.json  vcpkg.json
  config/          bot.toml  dex_programs.toml  denylist.json
  proto/           geyser.proto  solana-storage.proto   # Yellowstone protos, pinned commit
  src/
    core/          pubkey, base58, u256, compact_u16, expected, clock
    feed/          laserstream_client, subscription_manager, slot_clock
    state/         state_store, pool_state, account_decoders/
    dex/           quoter interfaces + raydium_v4, whirlpool, cpmm, dlmm, clmm, pumpswap
    engine/        route_graph, cycle_index, prefilter, sizer, cost_model, tip_policy, dispatcher
    flash/         kamino, marginfi, reserve_cache
    tx/            instruction builders per program, message_v0, alt_manager, signer
    submit/        jito_client, sender_client, bundle_tracker
    risk/          token_safety, limits, kill_switch, sign_guard
    obs/           metrics, decision_log, telegram
    tools/         discover, recorder, replay, sweep
    main.cpp
  tests/           unit/, parity/, golden_tx/, bench/
  program/         executor/ (Rust crate, cargo build-sbf)
  harness/         parity-node/ (official DEX SDKs, emits expected quotes as JSON)
```

**Libraries (via vcpkg)**

| Need | Library |
| --- | --- |
| LaserStream gRPC (Yellowstone protocol) | `grpc`, `protobuf` |
| HTTP/2 to Jito, Helius RPC | `curl` (multi interface, HTTP/2) |
| JSON parsing on hot paths | `simdjson` |
| Config | `tomlplusplus` |
| Ed25519 signing | `libsodium` |
| Hash maps, utilities | `abseil` |
| Lock-free queues | `concurrentqueue` (MPMC) and `rigtorp-spscqueue` |
| Logging | `spdlog` (async) |
| Metrics | `prometheus-cpp` |
| Tests and benchmarks | `gtest`, `benchmark` |

**Thread model**

| Thread | Pinned core | Work |
| --- | --- | --- |
| Feed (1 per gRPC connection) | Yes | Receive, decode, write `StateStore`, enqueue pool id |
| Engine (1) | Yes, isolated | Section 6 loop |
| Submit I/O (1) | Yes | Build, sign, send, persistent connections |
| Tracker (1) | No | Bundle status, outcome recording |
| Background (pool) | No | Discovery, ALT management, blockhash, tip floor, sweeps, metrics |

**Config (`config/bot.toml`, excerpt)**

```toml
[mode]
dry_run = true
funding_mode = "flash"          # flash | own

[helius]
laserstream_endpoint = "VERIFY" # from Helius dashboard, Frankfurt region
rpc_url = "https://mainnet.helius-rpc.com"
api_key_env = "HELIUS_API_KEY"
max_grpc_connections = 4

[jito]
regions = ["frankfurt", "amsterdam"]
tip_min_lamports = 1000
tip_max_lamports = 50000000

[engine]
base_mints = ["So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"]
max_hops = 3
min_spot_edge = 0.0005
min_ev_lamports = 20000
min_net_lamports = 10000
max_input_fraction = 0.20
target_land_rate = 0.30

[risk]
max_daily_fee_spend_sol = 0.2
min_sol_float = 0.3
max_sol_float = 2.0

[keys]
hot_wallet_path = "/etc/sol-arb/hot_wallet.json"
cold_wallet_pubkey = "VERIFY"
```

The wSOL and USDC mint addresses above are the standard mainnet mints; the agent must still confirm them at startup by reading the mint accounts.

## 13. Build phases, acceptance criteria and open items

Build in seven phases; nothing sends a mainnet transaction before phase 4, and nothing uses a flash loan before phase 5.

**Phase 0: Scaffolding (local)**

Core types, base58, `u256`, compact-u16, config loading, logging, metrics endpoint, CI with `-Werror`.

- [ ] Unit tests for base58 and compact-u16 against known vectors pass
- [ ] `bot --dry-run` starts, loads config, exposes metrics

**Phase 1: Feed, decoders, recorder (Raydium AMM v4 and Orca Whirlpool)**

- [ ] Discovery lists pools for the 2 DEXes and builds the token graph
- [ ] LaserStream subscriptions hold for 24 hours with automatic reconnect
- [ ] Decoded fields match SDK decode on 500 fixtures per DEX
- [ ] Recorder captures 3 days of updates; bandwidth is under the monthly credit budget

**Phase 2: Quoters and engine in replay (no sending)**

- [ ] Quoter parity is 100% on 1,000 snapshots × 20 sizes per DEX
- [ ] Engine meets the latency targets in sections 5 and 6.2 under replay at 1× speed
- [ ] Replay report lists detected opportunities per day, with size, gross profit and the estimated tip needed to win
- [ ] Report compares detections with arbs that actually landed on chain in the same slots

**Gate after phase 2:** if replay shows fewer than about 20 detections per day with a net EV above `min_ev_lamports` after a realistic tip, stop and adjust scope (more DEXes, long-tail pools) before writing send code.

**Phase 3: Executor program and transaction builder (local validator)**

- [ ] Serializer output is byte-identical to SDK golden transactions
- [ ] On a local validator with cloned mainnet accounts, a profitable 2-hop route succeeds and a tampered `min_out` reverts with the custom error
- [ ] Kamino flash borrow and repay work around the executor on the local validator
- [ ] ALT manager creates, extends and waits for activation correctly

**Phase 4: Mainnet live, own capital (0.1 to 0.5 SOL)**

- [ ] At least 20 landed bundles; realized output within 1 unit of the quote on each
- [ ] Zero landed transactions with a loss
- [ ] Kill switch and all section 10 limits tested live

**Phase 5: Flash loans live**

- [ ] Switch `funding_mode` to `flash`; at least 20 landed flash-funded bundles
- [ ] Live reserve fee and liquidity read correctly; disabled-reserve path tested
- [ ] Daily P&L after tips and fees is positive over 7 consecutive days, or the edge is re-assessed

**Phase 6: Scale**

Add CPMM, then DLMM, then CLMM and PumpSwap, each behind parity tests. Tune the tip controller per route class. Add a New York node. Add the marginfi fallback. Port the executor to native or Pinocchio if it started on Anchor.

**VERIFY checklist (resolve before the phase that needs it)**

- [ ] Program IDs and account layouts for every DEX in section 4 (phase 1)
- [ ] Exact fee formulas and rounding per DEX from program source (phase 2)
- [ ] LaserStream endpoint URL for Frankfurt, replay support and connection limits on the Business plan (phase 1)
- [ ] Kamino CPI restrictions and `calculate_flash_loan_fees` source (phase 3)
- [ ] Jito `sendBundle` encoding parameter, bundle status methods, rate limits, tip floor endpoint, and whether tip accounts may be in an ALT (phase 3)
- [ ] Helius Sender minimum tip, which sources disagree on (only if the Sender path is enabled)
- [ ] marginfi flash-loan fee and account setup (phase 6)

**Realistic expectations.** The atomic guard removes trade risk, not competition. Expect most detections on liquid pairs to be won by established searchers; the replay gate after phase 2 exists so you find that out before spending on mainnet.

## Sources

- [Helius: LaserStream on Business plans](https://www.helius.dev/blog/laserstream-websockets)
- [Helius pricing](https://helius.dev/pricing)
- [Helius docs for agents (Sender endpoint, recommended stack)](https://www.helius.dev/docs/agents/overview)
- [Chainstack: Jito bundles, tips and regions](https://chainstack.com/jito-explained-bundles-tips-mev-solana/)
- [Jito docs: block engine latency guidance](https://jito-labs.gitbook.io/mev/searcher-resources/block-engine)
- [Kamino Lend flash loans (program checks and fees)](https://www.mintlify.com/kamino-finance/klend/advanced/flash-loans)
- [klend-interface crate (flash instruction builders)](https://docs.rs/crate/klend-interface/0.6.0/source/src/instructions/flash.rs)
- [Asymmetric Research: marginfi flash-loan checks](https://blog.asymmetric.re/threat-contained-marginfi-flash-loan-vulnerability)
