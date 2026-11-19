# Feeds, replays and the live LP test (2026-11-19)

**Bottom line:** none of today's tests found an edge. Astralane shreds arrive later than Helius LaserStream, and
postpack is the same signal as Helius preconf at 2 % coverage. Helius preconf leads LaserStream by about +15 ms in
55 % of slots. Every curve-strategy variant lost in replay. The one live test, LP on fresh pools, lost 0.178 SOL over
4 positions and is paused.

Measured on the jito FRA VM (3.65.33.160), Helius LaserStream `processed` as the baseline (pump-desk's feed).

## 1. Early feeds on pump.fun traffic

**Method:** four-way run, 11:25–11:35 UTC: 257,445 pump.fun txs in 2,685 slots, joined by signature. Positive lead
means the feed was earlier than Helius LaserStream.

| Feed | Slots | Txs | Lead p10 / p50 / p90 | First | Cost |
|---|---|---|---|---|---|
| Helius `preconfSubscribe` | 54.9 % | 54.8 % | 2.5 / **+15.1** / 39.5 ms | 93.4 % | ~8.8M credits/h, unfiltered |
| Astralane postpack | 3.1 % | 2.0 % | 6.1 / +14.8 / 57.6 ms | 99.9 % | Astralane credits |
| Astralane shreds (tier 1) | 100 % | 100 % | −26.4 / **−12.4** / −4.7 ms | 2.7 % | 0.1 SOL / 24 h |

- **Postpack is a subset of Helius preconf.** On the 4,264 txs both feeds carried, they arrived within ±1 ms of each
  other: the same upstream source.
- **Recorder bug.** The postpack client's `Preconf::signature()` reads bytes 1..65, which is wrong for v1 txs, whose
  signatures sit at the end. `feeds/record_prog.rs` uses the parsed view instead.

Tools: `research/feeds/` (`record_prog.rs` and `helius_preconf.rs` are examples for Astralane's
`postpack-conf-client`; `deshred.py` decodes raw Merkle shreds; `join3.py`, `join4.py` and `pdjoin.py` join the feeds).

## 2. Replays on live-recorded data (`pd-collect`)

**Data source:** `research/collector/` (Rust) records every pump.fun event, migration and the canonical pool's
reserves for 30 min after each completion. `build_paths_from_capture.py` turns that into the `replay_curve.py` input
with no per-curve RPC.

Results from about 1 h of data (219 curves admitted within 5 SOL of completion; about 25 pools):

- **Be-the-completer** (`replay_completer.py`): **FAIL.** Most curves our size could finish never complete in
  reality: 73 at 0.5 SOL. The scored trades were lost races or stalls, at −50 % to −70 %.
- **Curve-in** at 0.05 / 0.25 / 0.5 SOL: test means −21 % / −28 % / −44 %. Every trade was a stall.
- **Follow profitable wallets** (`replay_options.py`): −16 % to −20 % over about 4,000 test trades.
- **Mid-curve momentum:** the best rules came out at about +2 % to +12 % on only 9 test trades. Not significant.
- **LP on fresh pools** (`lp_explore.py`):
  - The LP's cut is about 0.2–0.3 % of volume.
  - The fee income in the median pool was +0.2–0.3 % over 5–25 min.
  - With cheap txs: enter +5 s, hold 5 min, 8 % stop gave +0.94 % mean and −0.01 % median (n = 22).

## 3. Live LP test (`lp-bot/`)

| Closed (UTC) | Exit | P&L | Held |
|---|---|---|---|
| 14:09:34 | stop (zero-deposit bug: a lagging RPC read the bought tokens as 0) | −0.0181 SOL | 9 s |
| 14:19:51 | stop −10.7 % | −0.0262 SOL | 8 s |
| 14:20:57 | stop −10.1 % | −0.0342 SOL | 39 s |
| 14:28:35 | stop −23.6 %, filled at −39.7 % (collapse between 1 s marks plus a 3-tx exit) | −0.0992 SOL | 61 s |

**What went wrong:**

- **Entry timing.** The pool's first-seconds dump, about −12 % by +6.8 s, landed right after our +5 s entry. The
  8 % stop then sold into a dip that recovered within seconds.
- **Exit latency.** Tx-to-tx gaps on chain were 2–5 slots. The exit is now one tx; it was never exercised live.
- **Daily loss limit.** It latched after position 4. The owner then withdrew the wallet, and the bot is paused
  with the KILL file.

**Astralane GBX / Bifrost** (`fra-gbx.astralane.io:5064`): a Jito-compatible gRPC bundle gateway to the GBX
builders (Bifrost, Titan). Stake coverage is unpublished. At most about 1 slot saved, and only in the slots it
covers. Not tested.
