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
