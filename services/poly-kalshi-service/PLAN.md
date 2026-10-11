# poly-kalshi: improvement plan after the epoch-3 paper run

Written 2026-10-07 from the Dublin export `pk-export-dublin-dev-20261007T005037Z`
(build `1721a36`, config `148fd9c4ca`). It is meant to be worked through on the
research server that holds the tapes, then deployed to the Dublin paper host.

Work the phases in order. Each task says what to change, where, and how to know it
is done. Phases 1 and 2 are code that ships to Dublin. Phase 3 is research on the
tapes. Phases 4 and 5 end in decisions, not code, unless their numbers say otherwise.

Live orders now exist behind `PK_LIVE_CONFIRM=yes` (README, "Live execution"). This
plan does not turn them on: the gates decide that.

---

## 0. Where we are

### Settled decisions (2026-10-07)

- **Threshold stays at 0.10.** At 0.20 there were almost no fills. Because the first
  crossing decides, the threshold *defines* the trade, so 0.10 is a different
  strategy from the manual's 0.20. The manual's magnitudes ($/entry +4.4 to +6.9,
  +5 pp signal) do not apply to it, so the gates are re-registered (task 0.3).
- **Dublin is the paper host and the planned live host.** It is the closest location
  allowed to trade on Polymarket. London is close-only, so it can record but never
  trade.
- **The research server holds the Helix kpc tape** (`~/kpc/raw`, about 73 days,
  binary, best bid/ask only). Its decoder is missing (task 0.1a). Our own tapes
  (`pk_*.gz`) were recorded in London on 2026-10-06 and in Dublin after that.
  London-recorded tapes must be re-timed to Dublin before their fill numbers mean
  anything for Dublin (task 3.1).

### Epoch-3 baseline: Dublin, 2026-10-06 19:00 to 2026-10-07 00:50 UTC, 7 assets, gap 0.10

| | |
|---|---|
| Funnel | 161 windows, 123 crossings, 16 above-cap skips, 107 orders, 61 fills (57%), 58 settled |
| P&L | +$73.02 (gross $116.26, fees $43.24 = 37% of gross); profit factor 1.12; 25 wins, 33 losses |
| Drawdown | max $186.66, from an equity peak of +$253.90 |
| Size | **$9.58 filled per entry against $50 ordered**; 10 of 107 orders filled in full |
| Per asset, $ filled per entry | BTC 30.9; XRP 7.6; DOGE 4.9; ETH 3.3; BNB 2.8; HYPE 1.9; SOL 1.8 |
| BTC | 19 of 23 orders filled; −$43.6 P&L; $34.1 of the $43.2 in fees |
| Day-t | 1.68 over about 6 hours. **Nothing is significant yet.** |

The rest of the table is per entry, assuming a fill at the touch, net of fee:

| Group | n | Win rate | Net ¢/share |
|---|---|---|---|
| All entries | 104 | 50% | +14.0 (standard error 4.7) |
| **Filled** | 58 | **43%** | **+7.9** (standard error 6.5) |
| **Not filled** (ask moved up within 150 ms) | 46 | **59%** | **+21.7** (standard error 6.7) |
| Entered before second 120 | 23 | 35% | −7.6 |
| Entered from second 120 to 600 | 68 | 55 to 62% | +18 to +26 |
| Skipped above the cap | 16 | 69% at an average price of 0.71 | −3.2 |

**Fills look adversely selected.** The orders we miss are the winners: somebody
faster takes them. The gap is only about 1.5 standard errors, so phases 1 and 3
exist to measure it properly.

### System

- **Engine:** 29 µs at p50 and 123 µs at p99. It is not the bottleneck.
- **Feed latency:** Polymarket 9.8 ms at p50; Kalshi a flat 47 ms (the transatlantic hop).
  Both are measured as receive time minus the venue's timestamp, so they include
  clock skew.
- **Polymarket socket drops:** about 10 an hour, closed by Polymarket for falling
  behind (1013 "slow consumer"); 67 in the export.
  - Event-loop lag max 3.1 s.
  - Polymarket book age p99 1.03 s, max 2.4 s.
- **CPU:** 27% of 4 vCPU. One core is the ceiling, because of Python's global
  interpreter lock (GIL).
- **Lead and lag:** on the per-second series, Kalshi's move and Polymarket's move
  correlate 0.68 in the same second and 0.28 when Polymarket moves 1 s later. So the
  lead is under a second, and latency is the game.
- **Healthy:** Kalshi sequence gaps 0 since `1721a36`; venue disagreement 2.5%; the
  recorder dropped nothing; invalid legs 0.8%.

### Blind spots in the current data

- **The gap is censored at the first crossing.** `max_abs_gap` stops at the crossing
  (78 of 104 entries were at a gap of 0.10 to 0.11), so the export cannot say how far
  gaps run.
- **No depth at the decision.** Nothing records how many shares were at the touch.
- **No markout after fills.** Adverse selection can only be inferred from no-fills.

---

## Phase 0: inventory and baseline (research server)

### 0.1 Two tape families, two jobs

There are two kinds of tape, and they answer different questions:

| | Helix tape (`~/kpc/raw`) | Our tape (`pk_*.gz`) |
|---|---|---|
| Span | about 73 days | London hours on 2026-10-06, then Dublin from 18:43 UTC |
| Format | binary `.hlxz` (`HLX1` magic), one file per stream per day | gzip text, `<recv_ns> <venue> <raw frame>`, one file per hour |
| Polymarket | YES and NO **best bid/ask only**, about 159 ticks/s, 138 GB per side | full book (snapshots and deltas) |
| Kalshi | YES and NO best bid/ask, 53 GB per side | full book |
| Extra | executed trades on both venues; **Chainlink reference prices**; window definitions (strike, open time, token ids) | control lines (windows, resolutions, reconnects) |
| Readable here | **not yet: the decoder (`research.recv3`) is coming from Helix's owner** | yes, by `recordings.read_lines` |
| Good for | signal statistics at scale: threshold, timing, spread filter, null battery, regime split, fair value on Chainlink, trade-flow adverse selection | fills: depth, latency, limit offset, markouts, determinism against live |

The Helix tape has no depth, so it cannot say how much would have filled. Our tape
can, but it covers only days. So use Helix for *whether the signal is real and which
variant is best*, and our tape for *how much of it we can actually fill*.

**Disk:** the Helix tape is 368 GB compressed, and 122 GB is free. **Never write a
decoded copy of the tape.** Decode in a stream, and write only derived outputs
(0.1b).

### 0.1a Helix decoder

1. **Run `sha256sum -c kpc_tier2.sha256` first,** so corrupt files are known before
   any number depends on them.
2. **Use the original decoder.** Helix's owner is sending `research.recv3`, the
   module the scripts in `~/kpc/kpc_tick/` import from `/Users/cartasyl/...` (with
   `iter_poly_windows` among others).
   - Vendor it as `research/helix/recv3.py`, unchanged, with a header noting its
     source and the date received. Replace the hardcoded path with an argument or
     `HELIX_TAPE_DIR`, and change nothing else.
   - Read it, then write `research/helix/README.md` documenting:
     - the `HLX1` record layout;
     - which clock each timestamp is on;
     - whether sizes are present;
     - its public functions.
   - Wrap it in `research/helix/reader.py`, a streaming reader per stream and day
     that yields typed records: receive time, venue time if present, token or ticker,
     bid, ask, sizes if present. Everything else imports from this wrapper, never
     from `recv3` directly.
   - Measure decode speed on a full Polymarket day. If it can't do about 1B ticks in
     reasonable time over 4 vCPUs, add a chunked `numpy.frombuffer` path to the
     wrapper, and check it gives identical output to `recv3` on a sample day.
3. **Only if the file never arrives:** reverse-engineer the format from the `HLX1`
   header and how `kpc_tick/` uses the decoded fields, behind the same
   `reader.py` interface.

**Validate the decoder before using it:**

- Polymarket YES and NO mirror each other: `yes_bid ≈ 1 − no_ask` on almost every
  tick.
- Ticks fall inside their window's open-to-close span, taken from the Window stream.
- The final quotes converge to 1/0 on the side that resolved. Check that against
  Polymarket's recorded resolution for a sample of windows (Gamma, cached; send our
  own user agent).
- Tick rates match the manual: about 159 per second for Polymarket.
- Find out which clock the timestamps are on (receive time, venue time, or both) and
  which host recorded them. Re-timing (3.1) depends on it.

**Done when:** `research/helix/reader.py` decodes a full day of each stream, and every check
above passes.

### 0.1b Helix inventory and derived outputs (`research/helix_replay.py`)

- **`research/data/helix_inventory.json`**, one row per stream per day:
  - records and time span;
  - checksum result;
  - assets present;
  - largest internal time gap.
- **The settlement regime of each day.** Polymarket's 15-minute markets moved to
  Chainlink 60 s TWAP settlement at some date; find it from Polymarket's
  announcements or the Window stream. Days before and after are different markets,
  so every Helix result is reported per regime. The 0.10 gate is set from the
  post-TWAP days only.
- **The fee per window,** if the Window stream carries it. Otherwise find when the
  crypto 15-minute taker fee (rate 0.07) started, and apply it from that date.
- **`helix_replay.py`:**
  - merges the day's streams by time (a heap merge over the per-stream readers);
  - keeps a `Quote` per leg per window;
  - calls **the same `decide()`** for every variant (1.2's list), so research and
    live share one rule.
- **Settlement:**
  - Polymarket's resolution, fetched once per window and cached in
    `research/data/resolutions.parquet`;
  - Chainlink derived from the tape as a cross-check;
  - Kalshi's resolution, to measure venue disagreement.
- **Outputs, the only things written:**
  - `grid.parquet`: per window per second, as `replay.py` writes;
  - `crossings.parquet`: per variant per window, the first crossing with the touch
    and gap;
  - `slices.parquet`: for each crossing, the tick-level best bid/ask and trades from
    −2 s to +5 s.

  Expect roughly 1 to 3 GB for all 73 days, against 122 GB free.

### 0.1c Our tape inventory

- Write `research/data/inventory.json` with one row per hour:
  - host, lines per venue, sessions;
  - `out_of_order` and `truncated` counts;
  - whether the hour falls before `3bfa696` (ghost Kalshi levels) or `1721a36`
    (Kalshi sequence fix). Those hours are excluded from fill research, or at least
    flagged.

**Done when:** every hour of both families is classified as usable or excluded, with
the reason.

### 0.2 Replay determinism on a Dublin hour

- Copy one complete Dublin hour, plus that day's live journal, to the research server.
- Run `replay run` at the live parameters, then `replay compare`.

**Done when:** agreement = 1.0. Otherwise stop and fix it first: every later number
depends on it.

### 0.3 Re-register the gates for 0.10, before reading any new results

Edit the README's Gates section so the 0.10 strategy has its own pre-registered
targets:

- **Primary:** day-t ≥ 2 at n ≥ 300 *filled* entries at or below 0.60, net of fees,
  at the *real* fill (not the touch). Kill if not met.
- **Adverse selection:** the median 5 s markout (task 1.3) on fills is no worse than
  −3¢. If it is worse, the edge belongs to faster traders and we are their exit.
- **Capacity:** the median $ filled per entry is reported per asset. An asset under
  $10 at the touch can't carry live size, whatever its edge.
- **Null battery:** `signal` is above `random_same_rule` by a margin fixed now. The
  +5 pp target was for 0.20. Re-derive it from the Helix discovery days of the
  current settlement regime (phase 3), and write it down before running the
  holdout.

**Done when:** the README is updated and committed before phase 3 starts.

---

## Phase 1: observability (code, ships to Dublin)

None of this changes a decision, so it is deployed without bumping
`PK_RESULTS_EPOCH`. Readouts can split by `build` where needed.

### 1.1 Uncensored gap tracking (`engine.py`, window summary record)

Keep computing the gap after the window is consumed. It is cheap: only `decide()`'s
early exit is skipped. Add to the `window` record:

- `max_abs_gap_full`: over the whole entry window, not just up to the crossing.
- `first_cross_s`: the first second at which |gap| ≥ t, for each t in
  {0.08, 0.10, 0.12, 0.15, 0.20}.
- `gap_at_cross_side`: the side at each first crossing.

**Done when:** the window records show the full distribution, and a test covers a
window that crosses 0.10 and later 0.15.

### 1.2 Shadow parameter sets (`engine.py`, new `shadow.py`)

`decide()` is pure, so each tick can evaluate a list of variants with no orders.

- **Config:** `PK_SHADOW_VARIANTS`, a JSON list of
  `{name, gap, cap, early, late, max_kalshi_spread}`. Add `max_kalshi_spread` to
  `StrategyParams` with a default of `None` (off), so the live rule is unchanged.
- **Starting set:**
  - `t08`, `t12`, `t15`, `t20`: thresholds;
  - `early120`: entry_early_s = 120;
  - `kspread05`: skip when the Kalshi spread is over 0.05; about 20 of 104 entries
    had a Kalshi spread of 0.10 or more;
  - `cap50`: cap 0.50.
- Each variant keeps its own one-decision-per-window state.
- **Journal:** write a `shadow` record per variant per window:
  - the decision, the touch, the depth at the touch (task 1.3);
  - at settlement, the outcome and the net ¢/share at the touch.
- **Cost guard:** a variant whose window is consumed costs one early return. Check
  the `engine_us` p99 before and after; budget: p99 < 300 µs.

**Done when:** `research.readout` and the status page show a per-variant table
(n, win rate, average price, net ¢ at the touch, standard error).

### 1.3 Fill telemetry: depth and markouts (`engine.py`, `venue.py`)

- **On every enter decision:** record `depth_at_limit`, the shares offered at or
  below the limit price at the decision, and the same at fill time.
- **On every fill and every no-fill:** record the cheap leg's ask and mid at +1 s,
  +5 s and +30 s, i.e. the *markout*.
  - Keep a small pending list. Resolve it on the first event past each horizon, the
    same way pending orders are resolved now.
- **Readout:** show markouts for fills and no-fills side by side. This is the direct
  measure of adverse selection that the epoch-3 data only hints at.

**Done when:** an export's fills carry `markout_1s/5s/30s`, and the page shows
markouts for fills and no-fills.

### 1.4 Small fixes

- **`oldest_open_age_s` is negative** (−563 in the export). It should be the time
  since entry, or since the window closed, and never negative.
- **`realised_today` and the daily row disagree:** the ledger said −$65.98, the
  daily row −$63.18. One counts by settle day, the other by window day. Pick one
  (settle day, since that is when cash moves) and label it.
- **Documentation drift:**
  - `.env.example.dev`: `PK_GAP_THRESHOLD=0.10`, and `PK_HOST_TAG=dublin` with a
    Dublin checklist (the London notes become "recording only").
  - The README's first paragraph still says 0.20.
  - The replay CLI's defaults: `--gap 0.10`, and `--assets` covering all 7.
- **Kalshi discovery 429:** HTTP 429 three times during restarts.
  - Stagger the per-asset market queries, about 250 ms apart.
  - Retry with jittered backoff.
  - Reuse one series query per asset per roll.

---

## Phase 2: Polymarket feed stability (Python, before any rewrite)

The drops cost us crossings and leave books stale for up to 2.4 s. The engine is
fast; the cost is the single-threaded reader, plus GIL contention from the recorder
thread.

### 2.1 Shard the Polymarket subscription (`feeds.py`, `runtime.py`, `engine.py`)

- Use `PK_POLY_SOCKETS` sockets (default: one per asset). Each has its own reader
  task and its own reconnect.
- **A reconnect must invalidate only that shard's books.** Today a `reconnect`
  control line drops every Polymarket book. Add `shard` and the token ids to the
  control line, and drop only those books.
- The tape stays one stream (venue `p`), so replay is unchanged.

### 2.2 Take compression off the GIL (`recorder.py`)

- Pipe the lines to a `gzip -1` child process on its stdin, instead of compressing
  in a thread.
  - Same file format, so `recordings.read_lines` is unchanged.
  - Keep the hour rotation and the manifest.
  - On a crash, the child flushes what it has.
- An alternative is a `multiprocessing` writer fed raw bytes. Pick whichever is
  simpler to test.

### 2.3 Cheaper per-message work

- Use `uvloop` as the event loop.
- Sample the `price_change` best-bid/ask check, 1 in 50 for example (31M checks in
  the export). Keep the counter's meaning by scaling, or rename it as a sample.
- Profile one busy hour with `py-spy record` on the Dublin host. Attach the
  flamegraph to the PR.

**Acceptance for phase 2, over 24 h on Dublin:**

- `slow_consumer` ≤ 1 a day;
- `loop_lag_ms` max < 250 ms;
- `age_ms_poly` p99 < 150 ms;
- `engine_us` p99 < 300 µs (with the shadow variants on).

**If not met,** go to the 5.3 gateway, not a full rewrite.

---

## Phase 3: replay research on the tapes

### 3.1 Re-time London tapes to Dublin (`research/replay.py`, `research/helix_replay.py`)

This applies to our London hours and, if 0.1a finds Helix recorded in London (or
anywhere else that isn't Dublin), to the Helix tape too. Both replays take the same
`--shift-ms` option.

A London tape has Polymarket at about 1 ms and Kalshi at about 38 ms from the
venues. Dublin sees Polymarket at about 10 ms and Kalshi at about 47 ms.

- **The Kalshi-versus-Polymarket offset happens to be similar** (about 37 ms either
  way), so the signal timing barely changes.
- **What changes is the order path.** From Dublin an order reaches Polymarket about
  5 to 10 ms later than from London, so the effective latency must grow by that.
- **Tooling:**
  - Add `--shift-ms k=…,p=…,c=0` to `replay run`. It delays each venue's lines, then
    re-merges them by the new time (a heap merge of the shifted streams).
  - Count the lines whose order changes.
  - Report `feed_ms_k` and `feed_ms_p` in `summary.json`, so each tape's geometry is
    measured, not assumed.
- **Calibration:**
  - Measure feed_ms on a London hour and on a Dublin hour.
  - Set the shifts to the differences.
  - If both hosts ever record the same hour, compare the re-timed London replay
    against the Dublin replay of that hour. Decisions should match in at least 95%
    of windows; fills will differ.

### 3.2 Decide once, fill many (`research/fills.py`)

A decision doesn't depend on latency or the limit, so don't re-run the engine for
every combination.

- One replay per parameter set writes, for every enter decision, the cheap leg's ask
  ladder at each event for the next 1,000 ms (`fills.parquet`).
- A vectorised pass then fills every combination of:
  - latency ∈ {0, 25, 50, 100, 150, 250, 400} ms;
  - limit offset ∈ {0, 1, 2, 3}¢ above the touch, never above the cap.
- Check it against the engine: at 150 ms and offset 0 it must reproduce the replay's
  own fills exactly.
- **On our tape**, this uses the full ask ladder, so it measures size.
- **On the Helix tape**, the same pass runs on `slices.parquet`, but it can answer
  only *whether the ask was still at or below the limit after L ms*. That gives the
  price side of the latency curve over 73 days of n, with no size.
- **Helix trades give a proxy for adverse selection:** were there trades at or
  through our ask between the crossing and crossing + L? Those were other traders
  taking the liquidity we wanted. Check how well this proxy predicts the outcome
  against our tape's markouts (1.3) before relying on it.

### 3.3 The sweeps

| Sweep | Values | Tape | Question |
|---|---|---|---|
| Threshold | 0.08, 0.10, 0.12, 0.15, 0.20 | Helix (our tape after 1.1) | Uncensored: which threshold has the best net per window, and how often does each fire? |
| Entry start | 20, 60, 120, 180 s | Helix | Is the first-2-minutes loss real? |
| Kalshi spread filter | off, 0.03, 0.05, 0.10 | Helix | Is a gap measured on a wide Kalshi mid just noise? |
| Null battery | `research.nulls` arms | Helix | Is the signal above `random_same_rule`, per regime and per asset? |
| Latency, price side | 0 to 400 ms | Helix slices | Does the ask stay at or below the limit, per asset, over 73 days? |
| Latency, with size | 0 to 400 ms | ours | Fill rate and net ¢ at the *real* fill, per asset. **This decides whether speed (5.2, 5.3) is worth money, and whether BTC is playable.** |
| Limit offset | 0 to 3¢ | ours (price side also on Helix) | Does paying 1 to 2¢ above the touch win back the no-fill winners for less than their edge? |
| Sizing | $50 notional; fixed 100 shares; min(notional, depth at the touch) | ours for depth; Helix for variance | P&L variance, and how much one cheap BTC entry dominates. |

Run `research.nulls` on every arm that is promoted.

**Discipline:**

- **Discovery:** the Helix days of the current settlement regime, except the most
  recent 14.
- **Holdout:**
  - the most recent 14 Helix days;
  - our Dublin hours from after phase 1, for anything that needs depth.
- Report the earlier regime's days separately. They show whether the edge moved
  with the TWAP change, but never set a parameter.
- Promote at most two changes to the live parameters, and only if they hold on the
  holdout.
- A sweep winner that doesn't hold on the holdout is noise.
- Write the chosen comparisons down before running the holdout.

**Throughput:**

- **Helix:** about 1B Polymarket ticks over 73 days. Decode in chunks with numpy,
  run one process per day over the 4 vCPUs, and write only the outputs from 0.1b.
- **Our tape:** a Dublin hour is about 7M lines. Parallelise by day.
- Write everything under `research/data/<run>/`. Check free disk before each batch,
  and stop below 30 GB. The Helix tape is not ours to delete to make room.

---

## Phase 4: a faster signal (research; the route to playing BTC)

Kalshi is not the source of the move. It reacts to spot, as Polymarket does. The
traders who take our no-fill winners are probably reacting to spot or to Polymarket
flow, not to Kalshi 47 ms later. Matching them needs a faster signal, not a
different server.

**The Helix tape already holds Chainlink reference prices** (52 days). So 4.2 and
4.3 can start on history as soon as 0.1a works, without waiting for new recordings.
Chainlink is the settlement reference, but it is an oracle stream, not the fastest
spot feed. History therefore answers *whether a fair-value signal beats the gap*;
answering *how fast* still needs 4.1.

### 4.1 Record spot

- Add a record-only venue `s` to the tape: one or two spot feeds with low latency
  into Dublin. Measure the candidates' `feed_ms` from Dublin before choosing.
- Add, if it can be had, the Chainlink stream Polymarket settles on (the 60 s TWAP
  source), so live data and the Helix history match.
- No trading logic yet. The engine ignores `s` except for the grid.

### 4.2 Measure lead and lag at millisecond resolution

- Cross-correlate the event-time moves of spot, Kalshi mid and Polymarket mid, per
  asset.

**Question:** how many milliseconds does spot lead Polymarket, and does Kalshi add
anything once spot is known?

### 4.3 A fair-value shadow variant

- Compute P(up) = Φ(ln(S/K) / (σ·√τ)), where:
  - S is the spot price;
  - K is the window's opening reference;
  - σ is a short realised volatility;
  - τ is the time left.
- Run it as a shadow variant (1.2): enter when P(up) − ask > edge after fee.
- Compare it with the Kalshi gap on the same windows, especially BTC.
- Fit σ and the edge threshold on the Helix discovery days, using Chainlink as S and
  the Window stream's strike as K. Test on the holdout days, then live as a shadow
  variant.

**Decision:**

- **If spot leads Polymarket by more than our 150 ms:** the gap signal is stale by
  construction, and BTC is only playable on a spot or fair-value signal.
- **If spot does not lead Polymarket by that much:** BTC stays paper-only until the
  latency sweep (3.3) shows a positive net at the latency we can actually reach.

---

## Phase 5: location and stack (decisions, with the numbers to make them)

### 5.1 Location: stay in Dublin, but pick the best Dublin network

- **Why Dublin:**
  - Polymarket's CLOB is in London (AWS eu-west-2), and Dublin is the closest
    location allowed to trade.
  - Paris and Frankfurt are close-only (FR, DE), and London is close-only (UK).
- **The one lever left is the provider inside Dublin.** On the same cloud backbone
  as Polymarket (AWS eu-west-1), Dublin to London is typically about 10 to 12 ms
  round trip, with low jitter.
- **Benchmark before moving:**
  - Run a small probe for 24 h on the current host and on an AWS eu-west-1 instance.
  - Measure the round trip of an HTTPS request to the CLOB's server-time endpoint on
    a kept-alive connection (TCP and TLS set up once), at p50, p99 and max.
  - Measure `feed_ms_p` on the market websocket.
  - Don't use ICMP ping: if the API sits behind a CDN, a ping measures the CDN's
    edge, not the matching engine.
- **Move only if** the p50 improves by more than 2 ms, or the p99 by more than
  10 ms. The phase 3 latency sweep says what a millisecond is worth.
- **Other allowed EU locations:** none is closer to London than Dublin unless its
  jurisdiction is confirmed allowed. Check Polymarket's current geoblock list before
  considering, for example, the Netherlands. Even then it would gain only a few
  milliseconds.

### 5.2 Splitting Kalshi into Ohio (or Virginia): not worth it

Our notes put Kalshi's API in AWS us-east-1 (N. Virginia); Ohio is us-east-2.

| Path, Kalshi event to us | Approx. one-way |
|---|---|
| Kalshi (Virginia) → Dublin, direct (today) | about 35 to 40 ms of network; 47 ms measured, including Kalshi's own delay and clock skew |
| Kalshi → Ohio relay → Dublin | about 5 ms Virginia to Ohio, plus about 40 ms Ohio to Dublin: **worse** |
| Kalshi → Virginia relay → Dublin over the AWS backbone | about 0 to 5 ms better at best, plus a hop, a second clock and a second failure point |

- **A relay cannot beat the direct path.** The Atlantic is the fixed cost.
- **It can't send orders either.** The US is geoblocked on Polymarket, so the order
  must still leave from Dublin.
- **The one real benefit is offloading Kalshi parsing,** at about 640 messages a
  second. But Kalshi isn't the feed that drops; Polymarket is (phase 2).
- **Revisit only if** the phase 3 sweep shows that a few milliseconds move fill rate
  materially. Phase 4 is the bigger lever.

### 5.3 Rewrite in Go or C++: not now, and only part of it if ever (superseded: see the decision below)

- **The decision path costs 0.03 ms,** against about 50 ms of network and an assumed
  150 ms to the order. A rewrite of the engine saves under 1 ms.
- **Python costs us ingest throughput, not latency.** That is phase 2's job.

**Triggers, in order:**

1. **Phase 2 acceptance fails.** Build a small Go or Rust *ingest gateway*:
   - it handles both venues' websockets and the Polymarket shards;
   - it maintains the books;
   - it writes the tape;
   - it publishes normalised top-of-book to Python over a Unix socket.

   Python keeps decisions, the ledger, the status page and research. The tape
   format stays the same, so replay is unchanged.
2. **The 3.3 latency sweep shows fill rate and net rising sharply below about 50 ms,
   and Phase 3 (live) measures order signing in Python as a material part of the
   order path.** Move signing and order submission into the gateway.
3. **Neither happens.** Stay in Python.

**Decision, 2026-10-09: rewritten in Rust anyway, all of it.** The latency review
(build 07edbd4, epoch 6) agreed with the reasoning above: the engine is 0.044 ms and
the rest is network. It ranked the work as an AWS side-by-side test, then
instrumenting the real order path, then guarding stale and wide books, then (if
measured worth it) a gateway, and deferred a full rewrite. We rewrote the whole
service anyway, on the order the review set:

- **The order path first** (`pk-exec`, `pk probe-orders`). This is a real CLOB V2
  client: warm HTTP/2, L2 auth, EIP-712 signing, pre-signing as a gap nears the
  threshold, and per-order timing. It posts post-only orders below the bid and
  cancels them at once, so it measures decision → acknowledgement without a fill.
  Paper stays the venue of record, and `live` stays refused.
- **Stale and wide books** are live parameters (`PK_MAX_POLY_LAG_MS`,
  `PK_MAX_POLY_SPREAD`) and shadow variants, and `age_outlier` records trace each
  late book to its cause.
- **Everything else**: the trader, observer, status page, alerts, Telegram, and
  all of `research/` including Helix (`pk`, `pk helix`). Golden outputs from the
  Python service on fixed tapes are matched field by field in tests.
- The tape is now hourly zstd text files; the research commands still read the old
  `.gz` hours. Results restart at **epoch 7**.

What the rewrite does *not* change is the edge: n is still far short of the
300-fill gate, and faster code measures it better without making it larger. The
AWS comparison (`pk netprobe`, `infra/systemd/README.md`) is still to run on Dublin.

---

## Assets

- **Keep all 7 in paper.** Paper costs nothing, and the signal statistics need n.
- **Read capacity per asset.** At the touch, only BTC carried size (about $31 per
  entry). XRP is about $8. The rest are $2 to 5, too thin for $50 live, whatever
  their edge.
- **BTC:** the deepest, the most contested, and in epoch 3 the loser (−$43.6 after
  $34 in fees). It stays paper-only until either:
  - the 3.3 latency sweep shows a positive net at the latency Dublin can reach, with
    1.3's markouts no worse than −3¢; or
  - the 4.3 fair-value variant beats it to the move.
- **No new assets** until phase 2's acceptance holds. Each asset adds load to the
  socket that is already dropping.

---

## Order of work, and what each step unlocks

| Step | Unlocks |
|---|---|
| 0.1a | **Blocks all Helix research:** vendor `recv3` (being sent by Helix's owner), wrap it and validate it |
| 0.1b to 0.3 | Inventories, settlement regimes, Helix derived outputs; determinism; honest gates for 0.10 |
| 1.1 to 1.4 | Uncensored gaps, shadow variants and markouts accumulate on Dublin from now on |
| 2.1 to 2.3 | A clean feed; acceptance, or the trigger for the 5.3 gateway |
| 3.1 to 3.3 | Threshold, timing, spread filter and nulls over 73 Helix days; latency, limit offset and sizing with depth on our tape (holdout: the last 14 Helix days and Dublin after phase 1) |
| 4.2 and 4.3 on Helix Chainlink, then 4.1 | Whether a fair-value signal beats the gap (history), then whether a fast spot feed makes BTC playable (live) |
| 5.x | Location, relay and stack decisions, made with 3.3's numbers |
