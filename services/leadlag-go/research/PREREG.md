# leadlag pre-registration

This file fixes the data split, the definitions and the gate rules **before** the statistics they
govern are computed. Parts are appended in order and never edited after their statistics have run. A
change needs a new part that says what changed and why.

---

## Part 1: M3 signal study (written 2026-10-07, before any screen or event-study statistic)

### 1.1 Data and split

Source: data.binance.vision USDⓈ-M aggTrades, packed and verified by `llfetch`. Days that `verify`
flags with id gaps are excluded for every symbol (gate G0).

| Window | Dates (UTC, inclusive) | Used for |
|---|---|---|
| Screen | 2025-10-01 → 2026-01-31 | ranking the candidates and choosing the frozen followers; nothing else |
| G1 (train, outside the screen) | 2025-01-01 → 2025-09-30 and 2026-02-01 → 2026-03-31 | the G1 signal gate on the frozen followers |
| Validation | 2026-04-01 → 2026-06-30 | M4 only |
| Held out | 2026-07-01 → 2026-09-30 | M4, evaluated once |

G1 is measured outside the screen window, so choosing the best alts doesn't inflate G1. The
2025-10-10 crash is in the screen window. The G1 window has no comparable single day, and
M4's stability gate (G3) reports results with and without that day.

**Candidates.** These are the 30 USDT perpetuals listed before 2025-01-01, ranked by median daily quote
volume over the 30 days before 2026-10-07 (`llfetch candidates`; list in
`/data/leadlag/archive/screen-candidates.txt`). Ranking by today's volume favours coins that are big
now. That's accepted for a screen, and noted here.

**Leaders.** BTCUSDT is the **primary** leader: G1 is decided on BTC events. ETHUSDT events are
reported alongside BTC's, but they don't decide G1.

### 1.2 Observability (no lookahead)

The archive has only the trade time `T`. Live, Binance publishes an aggregate trade later, at `E ≥ T`.
Tokyo measured `E − T` p50 1–68 ms for BTC and ETH, and ~150 ms for most alts. The p90 is ~150 ms for
all of them (status page, 2026-10-07).

- A leader trade counts as observable at **`T + δ`**.
  - `δ` is the p90 of the leader's `E − T` over the first 24 h of Tokyo tape, rounded up to 10 ms.
    `llresearch delays` computes it and the value is frozen in `delays.json`.
  - Until 24 h of tape exists, the screen uses a provisional `δ = 150 ms`. G1 uses the frozen value.
- An event decided by the leader trade at `T_k` fires at `t_sig = T_k + δ`.
- A latency `L` then applies: the earliest action is at `t0 = t_sig + L`.
- Follower prices are ground truth at their own `T`.
  - The price at time `t` is the state after every follower trade with `T ≤ t`, including trades in the
    same millisecond. That works against us at entry.
  - A follower window that crosses a data gap (no follower trade for more than 10 min) is dropped.

### 1.3 Follower price

**Mid** = the latest follower trade price moved half a spread toward the middle: × (1 − hs) after a
taker buy (it printed at the ask), × (1 + hs) after a taker sell. Before any half-spread has been
observed, it is the last trade price.

- `hs` is an EWMA, weight 0.05, of (ask − bid) / 2 ÷ mid. It is sampled whenever the latest buy and the
  latest sell are both within 1 s and the book is not crossed.
- The (last buy + last sell) / 2 alternative was rejected before any real-data statistic was computed.
  The synthetic no-lookahead test showed that its stale side catches up after entry, which reads as
  +0.6 bp of false response.

**Half-spread estimate (for the screen's hurdle):** the median fresh sample, never below half a tick.

### 1.4 Leader events

| Item | Primary | Sensitivity (reported, does not decide G1) |
|---|---|---|
| Flow | Signed taker quote notional (USDT) in `(t − W, t]`: buys +, sells − | n/a |
| Window `W` | 500 ms | 100, 250, 1000 ms |
| Scale | √EWMA of the squared `W`-bucket flow on a fixed `W` grid, empty buckets counted, half-life 30 min; no events in the first 30 min after start or after a gap > 10 min | n/a |
| Trigger | `|flow / scale| ≥ θ`, crossing up from below θ | n/a |
| `θ` | 3 | 2, 4 |
| Cooldown | 1 s per leader and detector | n/a |
| Direction | sign of the flow | n/a |

### 1.5 Response

For every event and every follower:
- **Signed response, in bp:** `dir × (mid(t0 + h) − mid(t0)) / mid(t0) × 1e4`.
- **Pre-entry drift:** `dir × (mid(t0) − mid(T_k)) / mid(T_k)`, the part of the move that happened
  before we could act.

Grids:
- `L` ∈ {0, 5, 10, 20, 50, 100, 200} ms
- `h` ∈ {100, 250, 500, 1000, 2000, 5000} ms

**Confidence intervals** come from a day-block bootstrap: resample days with replacement, 2000
replicates, seed 1. The interval is the 2.5 and 97.5 percentiles of the pooled mean. A t-statistic
clustered by day is also reported.

**Placebo.** Exactly the same pipeline, with every event's `t_sig` moved **+1 h** and **+2 h**. Leader
flow is then unrelated to the follower window.

### 1.6 Screen

- **Run:** the primary event definition on the screen window, with `L` ∈ {0, 20} ms and
  `h` ∈ {250, 1000, 5000} ms.
- **Score per candidate:**
  - BTC events, `L = 20 ms`, `h = 1000 ms`;
  - the mean response's CI lower bound, divided by the round-trip hurdle;
  - hurdle = 2 × the taker fee (VIP 0 is 5 bp per side until M4 freezes the actual tier) + 2 × the
    half-spread estimate.
- **Freeze:** the **top 8 by score whose CI lower bound is above 0**, fewer if fewer qualify. They are
  written to `research/prereg.json` with this file's sha256 before G1 runs.
- **Fewer than 3 qualify:** G1 can't pass, and the result is written up as a stop.

### 1.7 Gate G1 (decided once, on the G1 window, primary definitions, BTC events)

Let `L*` be the smallest grid `L` at or above the Tokyo probe's warm REST RTT p50, plus 2 ms of
feed-side processing. G1 **passes** when all of these hold:

1. For **≥ 3 frozen followers**, the response at `L*` and the **primary horizon `h = 1000 ms`** has a
   bootstrap CI lower bound **> 0**.
2. For those same followers, **both placebos** have a CI that contains 0, or an absolute mean below a
   third of the real mean.
3. The synthetic-injection test passes in CI (it recovers a known lag; its placebo is null).

Other horizons, the ETH events and the sensitivity grid are reported but don't rescue a failure.
**If G1 fails, the project stops** and RESULTS.md records why. The recorder keeps running for the
quote tape.

**Out of scope for G1:** fees, fills and the decision rule. Those are M4 (gates G2–G4).

---

## Part 2: exploratory shadow trading (written 2026-10-08, before any shadow trade)

### 2.1 Why, and what it may not be used for

The plan runs shadow trading only after M4 freezes the parameters (README, Sequencing). This part starts
an **exploratory** shadow run before G1, to exercise the live path (signal → simulated taker fill →
exit → record) and see the live signal rate. The cost is that its trades are observed before the gates.
So:

- Every trade carries `"exploratory": true` and the run label **`explore-1`**.
- No exploratory trade, or anything computed from one, is used for the screen, the freeze, G1, G2–G4 or
  G5, or to choose any parameter of a later part. The M3/M4 work in Part 1 and later parts uses only the
  archive.
- G5 (live shadow, ≥ 3 weeks, n ≥ 200) still starts at the M4 freeze, with the frozen parameters, on the
  measured AWS Tokyo host.
- Changing any value below means a new part and a new run label. Results under one label are never
  pooled with another's.

### 2.2 The fixed run (`explore-1`)

| Item | Value | Source |
|---|---|---|
| Host | the ofi VM, Azure **japanwest (Osaka)**, shared with the ofi services | DEPLOY.md, Shared host |
| Leaders | BTCUSDT, ETHUSDT, each with its own detector; both trade, reported per leader | Part 1 §1.1 (BTC primary) |
| Followers | the 12 recorded followers in `LL_FOLLOWERS` (SOL, XRP, DOGE, SUI, BNB, ZEC, NEAR, UNI, ENA, WLD, 1000PEPE, ADA) | not screened; reported per follower |
| Event | `research.PrimaryDetector()`: W 500 ms, θ 3, cooldown 1 s, half-life 30 min, warm-up 30 min, gap reset 10 min | Part 1 §1.4 primaries |
| Signal time | the receive time of the leader trade that crossed θ (live receipt already includes Binance's publish delay δ) | Part 1 §1.2 |
| Latency `L` | **20 ms**: the smallest Part 1 grid value at or above this host's warm REST RTT p50 (10.75 ms) + 2 ms. AWS's L* = 5 ms stays the G1 value | probe 2026-10-08 14:44–15:14 UTC |
| Entry | first event received at or after signal + `L`: taker buy at the follower's ask (sell at its bid) from the latest bookTicker | taker strategy (README) |
| Skips | the follower already has a pending or open position; no quote; quote older than 1 s; crossed book; trader halted | |
| Exit | first event received at or after entry + **1000 ms**, taker at the opposite side | Part 1 §1.5 primary horizon |
| Size | **$25** notional per trade, quantity = 25 / entry price (not rounded to the lot step; bp results are unaffected) | user decision |
| Fees | **5 bp per side** (VIP 0, no BNB discount): 10 bp per round trip | Part 1 §1.6 hurdle basis |

Each trade records the leader, follower, direction, z, signal/entry/exit receive times, prices, the entry
spread, gross and net bp and net USD. The trader sends it through the ring to the observer, which
appends it to `/data/leadlag/shadow/<UTC day>.jsonl`. Replaying the tape with the same params file
(matched by sha256) must reproduce every trade (`deploy/jobs/shadow-verify.sh`).

### 2.3 Reported, not decided

Per follower, per leader and in total: trades, mean gross and net bp, hit rate, t-statistic of net bp,
net USD, and skips by reason. These are descriptive. No pass/fail rule applies to `explore-1`.

### 2.4 Steps skipped by starting now

- The README's sequencing rule (no exploratory shadow before the M4 freeze), deliberately waived under
  §2.1.
- M3: δ from 24 h of tape (shadow doesn't need it live), the screen, the freeze, G1.
- M4: the policy design, fill-model validation, the backtest, validation and held-out windows, G2–G4
  (not yet defined), the parameter freeze, and the actual fee tier.
- Host: the AWS AZ probe and the dedicated c7i host. Numbers here describe Osaka → Tokyo with an unpinned
  trader.
- The exit rule is a fixed 1 s time exit with no stop or take-profit: a measurement, not a strategy.

---

## Part 3: explore-1 as run, its result, and exploratory run `explore-2` (written 2026-10-09, before any explore-2 trade)

### 3.1 explore-1 as it actually ran

Part 2 §2.2 fixed the Osaka host and `L` = 20 ms. explore-1 actually ran on the **AWS Tokyo host**
(t2.xlarge, apne1-az1) from 2026-10-08 18:22 UTC, with **`latency_ms` = 5**. Everything else matched
§2.2. This part records the change after the fact. explore-1 still counts toward no gate.

### 3.2 explore-1 result (2026-10-08 18:22 → 2026-10-09 12:20 UTC)

1,748 signals and 20,976 trades over 12 followers. Follower mid response is signed in the signal's
direction, measured from signal + 5 ms on the bookTicker tape. The control is the same signals moved
+1 h.

| Horizon | Mid response | Control | Taker in, taker out (net) | Maker in, maker out (net, both fills assumed) |
|---|---|---|---|---|
| 100 ms | +0.61 bp | +0.01 | −10.6 | −2.2 |
| 1 s | +0.79 | +0.02 | −10.4 | −2.0 |
| 30 s | +1.35 (peak) | −0.03 | −9.9 | −1.4 |
| 5 min | +0.42 | +0.23 | −10.8 | −2.4 |

- **The response is real, and it is too small.** No execution mode clears VIP 0 fees.
- **Half the move comes before entry.** Followers had already moved +0.69 bp in the 500 ms before entry.
- **Flow z doesn't scale the response.** At 1 s, |z| ≥ 10 gives +0.86 bp and |z| 3–4 gives +0.81 bp.
- **The leader's own price move does.** Over the last 1 s, a leader move of 0–1 bp gives +0.99 bp at
  30 s; 2–4 bp gives +2.55; 4–8 bp gives +4.09 (n = 156).
- **So does how far the follower lags.** The gap is the leader's move minus the follower's, over the
  last 1 s. A gap of 2–4 bp gives +1.82 bp at 30 s.
- **The follower's spread decides its gross result.** ADA's 4.2 bp spread costs about 5× the response.

### 3.3 explore-2

**§2.1 is broken here, knowingly.** Its parameters were chosen by looking at explore-1. So explore-2 is
**exploratory**:
- it counts toward no gate;
- it is never pooled with explore-1;
- it doesn't replace M3. The price-move hypothesis still has to pass G1 on the archive before any
  freeze.

| Item | Value |
|---|---|
| Host | AWS Tokyo (as in §3.1) |
| Leaders | BTCUSDT, ETHUSDT, each with its own trigger |
| Followers | SOL, XRP, DOGE, SUI, BNB, ZEC, UNI, ENA, 1000PEPE. ADA, WLD and NEAR are still recorded but don't trade: their 2–4 bp spreads cost more than the response. |
| Trigger (`price`) | A leader's bookTicker mid moves ≥ **4 bp** against its mid **1000 ms** earlier. It fires on the upward crossing of that threshold, re-arms below it, and has a **5 s** cooldown per leader. No warm-up. |
| Follower selection | The gap is `dir × (leader move − follower move)` over the same 1000 ms. A follower needs a gap ≥ **2 bp**, and the **top 3** by gap are opened. Followers with a position are skipped (`busy`). |
| Entry | Taker at the far touch, **5 ms** after the signal. It is skipped if the spread is over **1.5 bp** (`wide_spread`). |
| Exit | At **30 s** after entry, a limit order at our own touch: a long sells at the ask, a short buys at the bid. |
| Maker fill rule | It fills when the opposite best price reaches the limit, or a follower trade prints strictly through it. This is optimistic: queue position isn't modelled. Otherwise the order crosses as a taker after **2 s**. |
| Fees | 5 bp taker, 2 bp maker (VIP 0) |
| Size | $25 notional |
| Markouts | The follower mid at +100 ms, 1 s, 5 s, 10 s and 30 s after entry, signed, in bp from the entry mid. Recorded on every trade. |

The status page reports the following, all descriptive, with no pass/fail rule:
- results by leader move (4–8, 8–15 and 15+ bp);
- results by exit kind;
- the mean markout curve;
- skips by reason.

The trades are also reported with a taker exit (`exit_kind` = taker, after a timeout), so the maker
fill model's optimism is visible.

**Replay.** `deploy/env/shadow-explore-2.example.json` is the params file. A replay of the tape with
it must reproduce the journal (`deploy/jobs/shadow-verify.sh`).

**Before the explore-2 start, an in-sample replay** used the same tape explore-1 was chosen from, with
`--shadow-all`. It is a code check, not a result. Its numbers:

| Measure | Value |
|---|---|
| Signals / trades | 216 / 325 |
| Exits filled as maker | 33% |
| Mean gross | +1.10 bp |
| Mean fees | 9.0 bp |
| Mean net | **−7.9 bp** |
| 30 s markout | +2.03 bp (explore-1: +1.35) |

Even in-sample, explore-2 doesn't clear its fees. It is a measurement run.

With the new binary, explore-1's journal replays exactly: 21,456 trades, none extra or missing.

---

## Part 4: exploratory runs `explore-3a`–`explore-3d` (written 2026-10-09, before any explore-3 trade)

### 4.1 Why

explore-1 and explore-2 put the follower response at +0.6 to +4 bp, while a taker round trip on USDT-M at
VIP 0 costs ~9–10 bp (Part 3 §3.2–3.3). The external review of 2026-10-09 (comparable studies, Binance fee
schedule) concludes that this edge can only be a **maker** edge:
- quote passively;
- use the leader signal to choose which side rests and when to pull it;
- trade the **USDC-margined** follower contracts, where secondary sources (2026) report 0 bp maker /
  4 bp taker for regular users. That is a promotion that no primary Binance page confirms.

Published last-in-queue maker tests on Binance USD-M lose 0.4–0.8 bp per fill to adverse selection
before fees. So these runs test one thing: whether the signal keeps adverse selection on our fills
below the spread we earn. That question needs fill-level markouts, not signal-level ones.

Like Parts 2 and 3, these runs:
- are **exploratory**: their parameters come from looking at explore-1/2 and the review;
- count toward no gate;
- are never pooled with explore-1, explore-2 or each other;
- do not replace M3/M4.

explore-2 keeps running unchanged next to them, in the same trader. Each run has its own params file and
run label, and its own aggregates.

### 4.2 Fees (checked before the start)

`llprobe fees --symbols <the USDC followers>,SOLUSDT` returns the account's `commissionRate`. Its output
is appended to this section as 4.2a **before** the first explore-3 trade. The params files below assume
0 bp maker and 4 bp taker on USDC-M:
- If the account's rates differ, `maker_fee_bp` and `taker_fee_bp` are set to the returned values
  before the start. That is the only change allowed, and it is recorded in 4.2a.
- Every record keeps `gross_bp`, so net results can also be recomputed offline at USDT-M rates
  (2 bp / 5 bp). The run is then judged at the account's actual rates.

### 4.3 Shared definitions

- **Leaders.** BTCUSDT and ETHUSDT, the bookTicker mid. **Price trigger:** as in Part 3 §3.3 (≥ 4 bp
  move against the mid 1000 ms earlier, an upward crossing, 5 s cooldown per leader).
- **Liquidations.** The trader now subscribes `<leader>@forceOrder`. Binance pushes at most one
  liquidation per symbol per 1000 ms, so this is a coarse flag, not a timing signal. A liquidation
  counts when its average price × filled quantity is ≥ `liq_min_usd` (50,000 USD). Its direction is
  the order's side (a SELL liquidation closed a long). With `liq_window_ms` set, a qualifying
  liquidation **against** the move within the window skips the signal (`liq_opposite`).
- **β-adjusted gap** (`"beta": "rolling"`):
  - β is an EWMA regression through the origin of the follower's 1 s mid log returns on the leader's,
    on a 1 s grid sampled from the receive-time mid histories;
  - half-life 30 min; warm-up 30 min, during which followers are skipped (`beta_warmup`); the estimate
    starts over after 10 min without events;
  - **gap** = dir × (β · leader move − follower move) over the trigger window;
  - **residual:** the same quantity, measured from the window start to now.

  `"beta": "one"` (the default) is explore-2's β = 1. explore-2's trades replay unchanged.
- **USDC followers.** The USDC-M versions of the explore-2 followers that are listed: SOLUSDC,
  XRPUSDC, DOGEUSDC, SUIUSDC, BNBUSDC, ZECUSDC, NEARUSDC (`llfetch candidates --quote USDC`). They are
  recorded from the start of explore-3.
- **Tick sizes.** They sit in each params file (`llfetch ticks`). The trader refuses to start if they
  differ from exchangeInfo.
- **Size.** $25 notional. **Latency:** 5 ms (Part 3 §3.1) for every order, cancel and reprice.

### 4.4 Queue model (quoter runs)

Each simulated post-only (GTX) order is handled as follows:
1. It reaches the book `latency_ms` after the decision.
2. If it would cross, it is **rejected**.
3. Otherwise it joins **behind the whole displayed quantity** at its price (bookTicker).
4. If its price is inside the spread, nobody is ahead of it.
5. If its price is behind the touch, its queue is unknown until the touch comes to it. It then joins
   behind the quantity displayed at that moment.

Fills:
- **Only trades** move the order up the queue: aggressor trades at our price use up the quantity ahead
  of it. Cancellations ahead are ignored.
- It fills when the traded quantity exceeds what is ahead of it, or at once on a trade strictly
  through its price.
- If the touch moves behind our price with no trade, the order is alone at the best price: nothing is
  ahead of it, but it still needs a trade to fill.

Cancels and reprices:
- Both take effect `latency_ms` later. Until then the order can still fill, which models a stale quote
  being picked off.
- A reprice loses the queue.

This is the conservative (risk-averse) model. It cannot be validated without real orders, so fill
rates and markouts are lower bounds on toxicity at best. Live $25 orders are a later, separate step.

### 4.5 The runs

| | explore-3a (quoter, arm A) | explore-3b (quoter, arm B) | explore-3c (Secondary 1) | explore-3d (Secondary 2) |
|---|---|---|---|---|
| Followers | USDC (4.3) | USDC (4.3) | USDC (4.3) | explore-2's 9 USDT followers |
| Signal | price trigger + β-gap ≥ 2 bp; opposite liquidation skips | none (quotes always) | leader ≥ 8 bp, or ≥ 4 bp with a same-direction liquidation; β-gap ≥ 4 bp; opposite liquidation skips | explore-2 unchanged (β = 1, gap ≥ 2 bp) |
| Selection | top 3 by gap ÷ spread; spread ≤ 4 bp; 5 s cooldown per follower after flat | every follower, spread ≤ 4 bp | top 3 by gap; spread ≤ 4 bp | top 3 by gap; spread ≤ 1.5 bp |
| Entry | post-only at our touch on the signal side (bid on an up move) | post-only bid and ask at the touch, following the touch when it moves away | taker (4 bp) | taker (5 bp) |
| Entry cancel | touch moves away > 1 tick: reprice once, then cancel (`chased`); leader gives back 2 bp from its signal mid (`reversal`); residual ≤ 0.5 bp (`gap_closed`); 30 s (`timeout`) | a leader move ≥ 2 bp over 1 s pulls the side it would run over (asks on an up move); it comes back when every leader's move is < 2 bp and 1 s has passed | n/a | n/a |
| Exit | post-only target at entry ± max(1 tick, 0.6 × gap); moved to our touch if that is more aggressive once the residual is ≤ 0.5 bp | the opposite quote (inventory ≤ 1 unit) | maker at our touch once the residual is ≤ 0.5 bp, or at 30 s; taker 2 s later if unfilled | same as 3c (no stop) |
| Stop / timeout | follower 1.5 × gap against the fill, or 30 s after the fill: cancel the exit, then taker | 10 bp against, or 30 s: same | follower 1.5 × gap against the entry mid: taker | none / 30 s |
| Fees (assumed, 4.2) | 0 maker / 4 taker | 0 / 4 | 0 / 4 | 2 / 5 (USDT-M) |

The params files are `deploy/env/shadow-explore-3{a,b,c,d}.example.json`.

### 4.6 Recorded and reported (descriptive)

**Every trade** records:
- `exit_reason`: timeout, gap_closed, target or stop;
- `exit_kind`;
- for quoter runs: `arm`, `entry_kind`, `queue_ahead` at the fill, `fill_wait_ms`, the markouts
  from the **entry fill price**, and `exit_markouts_bp` from the exit price. Those two are the per-leg
  adverse selection.

**Every arm-A entry attempt**, filled or not, is a `kind: "attempt"` record with:
- its end reason;
- the queue ahead when it joined;
- markouts from the **post** (1/5/10/30 s).

Filled versus unfilled attempts measure adverse selection by construction (fill probability against
post-fill return).

**The status page reports, per run:**
- the fill rate;
- the taker-exit fraction **f**;
- net per round trip;
- results by exit kind, by exit reason and by leader move;
- the markouts of filled versus unfilled attempts;
- skips.

**Control:** `leadlag --replay <tape> --shadow-params <file> --shadow-all --leader-shift 1h` re-runs any
of these with the leaders' events delayed 1 h. It is reported next to each run.

### 4.7 Kill criteria (each run separately; no pass rule)

A run is **stopped** when any of these holds:
- after ≥ 500 completed round trips (quoter runs: filled round trips), the mean net is < 0 and the
  upper end of its 95% day-block bootstrap CI is < +0.5 bp;
- quoter runs only: f > 0.4 over the same trades;
- after 2 weeks, fewer than 100 round trips (the run cannot answer its question; it is reported and
  stopped).

Otherwise each run continues to ≥ 2 weeks and ≥ 500 round trips and is then written up. The verdict is
descriptive: no explore-3 result unlocks a gate.

A run that survives is a reason to:
- design a pre-registered maker study on the archive plus the USDC tape;
- then test with real $25 post-only orders.

It is not a reason to trade.

## Part 5: one trader, `explore-4` (written 2026-10-14, before any explore-4 trade)

### 5.1 Why

From 2026-10-09 to 2026-10-14, explore-2 and explore-3a–3d ran side by side. Every run is net negative at its own
fee basis (status snapshot of 2026-10-14, build b3b9f98):

| Run | Round trips | Mean net bp | t | Taker-exit share |
|---|---:|---:|---:|---:|
| explore-3a (quoter A) | 831 | −2.87 | −11.7 | 43% |
| explore-3b (quoter B) | 244,817 | −2.51 | −228 | 26% |
| explore-3c | 351 | −5.93 | −9.8 | 52% |
| explore-3d | 2,318 | −8.37 | −57 | 58% |
| explore-2 | 2,112 | −8.64 | −33.7 | 63% |

explore-3a decomposes into three leaks:
- **Taker exits.** Its 472 maker exits average +2.52 bp net; its 359 taker exits average −9.95 bp.
  301 of those are stops (−10.6 bp on average): the 1.5 × gap stop turns every failed prediction into
  a 4 bp fee plus the spread, right after an adverse move.
- **Adverse selection.** Filled attempts mark out at +0.6 / +0.1 / −0.5 / −0.1 bp (1 / 5 / 10 / 30 s
  from the post). Unfilled ones mark out at +2.3 / +2.8 / +2.8 / +2.1 bp: the fills are the bad half.
- **Fill model.** Part of that gap may be the simulator. aggTrade is published ~150 ms late (Part 1
  §1.2), and the §4.4 queue model counted a late-published trade even when it had executed before our
  order reached the book.

3b says a lower taker share alone is not enough (26% and still −2.5 bp). So explore-4 changes the
three leaks together and keeps everything else as 3a. It is **one run** in **one trader**: the other
engines (taker entry, flow trigger, arm B) are deleted from the code, and there are no more parallel
epochs.

explore-4 is exploratory: its changes come from looking at explore-3. It counts toward no gate, is
never pooled with an earlier run, and does not replace M3/M4.

### 5.2 What changes from explore-3a

1. **Fill model (all orders).** §4.4 holds, plus: a trade counts toward our queue or fill only if its
   exchange time T is at or after the order reached the book (`T × 10⁶ ≥ live_ns`, at millisecond
   resolution, so a trade in the joining millisecond counts as before). A reprice restarts that
   clock. The local clock is Amazon Time Sync, measured at about −0.004 ms from Binance's.
2. **Entry gate on the queue (microprice).** Our side's share of the follower's L1 size is
   bid ÷ (bid + ask) for a long, ask ÷ (bid + ask) for a short. An entry needs a share ≥ 0.5
   (`min_entry_imb`): the microprice is at mid or on our side. A working entry is cancelled once its
   share falls under 0.25 (`cancel_imb`, end reason `imbalance`).
3. **Exit ladder.**
   - The 1.5 × gap stop becomes a **soft stop**: the exit moves to our own touch (reason
     `stop_soft`) instead of crossing.
   - A **hard stop** of 12 bp adverse (`hard_stop_bp`) crosses as a taker (reason `stop`).
   - At the 30 s **horizon** the exit moves to the touch (reason `timeout`) and crosses only 10 s
     later (`exit_grace_ms`).
   - An exit at the touch follows it as it moves away (the queue is lost on each reprice).

Unchanged from 3a: leaders BTCUSDT and ETHUSDT; the seven USDC followers; the price trigger (4 bp in
1 s, 5 s cooldown); the rolling β-gap ≥ 2 bp; the opposite-liquidation skip; top 3 by gap ÷ spread;
spread ≤ 4 bp; the entry cancels (one reprice, then `chased`; 2 bp reversal; residual ≤ 0.5 bp;
30 s); the target at max(1 tick, 0.6 × gap); $25; 5 ms latency; markouts at 1 / 5 / 10 / 30 s; fees
0 bp maker / 4 bp taker.

Not changed, and why:
- The 1 s `max_quote_age_ms` stays. bookTicker pushes only on a change, so an old quote on a quiet
  book is still the current book; feed liveness is the feed's `up` state.
- No follower is dropped. None is distinguishable from the others yet.
- No parameter was tuned on the tape. The four new values (0.5, 0.25, 12 bp, 10 s) are set from the
  explore-3a numbers and first principles, not fitted.

### 5.3 Params

`deploy/env/shadow-explore-4.example.json`, installed unchanged as `/home/ll/leadlag/shadow.json`.
sha256 `dec317919995e9d47395e7ac02a0e1f30e6623f2e6f0a84da5a42e041bd5704e`. The trader logs the sha256
at start; a different one is a different run.

### 5.4 Fees (checked at the start)

The 0 bp maker rate is still unverified: Part 4's 4.2a was never recorded. `llprobe fees` is run at
the deploy and its output appended here as 5.4a **before** the first explore-4 trade. If the account's
USDC-M rates differ, `maker_fee_bp` / `taker_fee_bp` are set to them; that is the only allowed change,
and the new sha256 is recorded in 5.4a.

### 5.5 Recorded and reported

As §4.6, plus:
- every attempt and trade records `entry_imb`, our side's share at the post;
- exit reasons now include `stop_soft`;
- the status page shows the latest 200 trades' mean net and taker share, taker crosses by reason
  (hard stop, grace ran out), the filled − unfilled 5 s markout, and imbalance skips and cancels.

Control: the +1 h placebo of §4.6, run once at the verdict.

Comparison with explore-3a is descriptive only. 3a ran on the old fill model, so it is not a
like-for-like control.

### 5.6 Verdict (`llresearch shadow --run explore-4 --part 5`)

The primary metric is the mean net bp per round trip at the account's fee rates, with its 95%
day-block bootstrap CI (round trips pooled by UTC day of the exit). In order:
1. **Kill** at any time once n ≥ 200, if the taker-exit share is > 25%.
2. Before **≥ 7 days and ≥ 400 round trips**: continue.
3. **Kill** if the CI's upper end is < 0.
4. **Pass** if the CI's lower end is > 0.
5. Otherwise **extend** once, by 7 days, and apply 3–4 again. Still undecided at 14 days: **stop**,
   inconclusive.

A pass is a reason to test the same rules with real $25 post-only orders on a fill-calibration
budget. It is not a reason to trade size. A kill means the passive-quoting hypothesis on these
followers is rejected: no further parameter search on it.
