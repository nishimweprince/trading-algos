# The Helix kpc tape

`~/kpc/raw` (`HELIX_TAPE_DIR`): 73 days of 15-minute up/down markets on both venues,
recorded by Helix's recorder, 2026-07-21 to 2026-10-01 (Kalshi from 07-13, with a
gap on 07-21 to 07-25). About 368 GB, zstd-compressed. **It is read in a stream and
never copied:** everything here writes derived outputs only.

| Module | |
|---|---|
| `recv3.py` | Helix's own decoder, vendored unchanged (header lines only). Never imported directly. |
| `reader.py` | The one entry point: typed records through `recv3`, and the fast array path. |
| `validate.py` | PLAN 0.1a's checks on a day: `python -m research.helix.validate --day 20260920` |
| `resolutions.py` | Each window's settlement from the venues (Gamma, Kalshi), cached. |
| `gamma.py` | One window's Polymarket outcome from Gamma, cached. |

`research/helix_replay.py` runs the live `decide()` over every window.

## Container

A day file is `<Stream>/<YYYYMMDD>.hlxz`: a 32 B `FileHeader` (magic `HLX1`,
version 1), then blocks of a 40 B `BlockHeader` (magic `HLXB`, type tag, record
size, count, compressed and raw length, first and last ts), each followed by one zstd
frame of same-tag, same-size records. A recorder restart appends a new FileHeader
to the same file (07-21: 8 sub-files), so the reader walks blocks inline and
resyncs at each header. A footer index is used only for single-sub-file days,
as in `recv3`. Records sit in **block order, not time order**: the arrays are
sorted on read.

## Records

Timestamps are int64 ns. Tags 22+ are ns at write time. Legacy tags (≤ 21) are µs and
`recv3` scales them. Tags 43+ (W) add a u16 `asset_wide` in what was padding.

| Stream | Tag (size) | Fields used | Notes |
|---|---|---|---|
| `Polymarket_Window` | 32/46 (256 B) `PolyWindow` | `open_unix`, `token_yes`, `token_no`, `strike`, `asset` | 5-minute windows (even asset ids) too; a restart repeats records (07-26: 707 for 672), `reader.iter_windows` keeps one |
| `Polymarket_Window` | 33/47 (96 B) resolution | `<QQ 64s d B B H 4x>`: recorded ts, `open_unix`, condition id, **close price**, 0xFF, asset, asset_wide | **Not decoded by `recv3`**; `reader.iter_resolutions`. The price is the next window's strike in every joinable window (100%), recorded ~4 s after close. It is Helix's derivation, not Polymarket's settlement (below). |
| `Polymarket_CLOB_{Yes,No}_BBO` | 27 (224 B) `PolyClobBbo` | recv, wall, pub, bid/ask price and **size** (e8 fixed point), condition id, token id, outcome | best bid/ask and the size at each, per token |
| `Polymarket_CLOB_Trade`, `…_No_Trade` | 25 (208 B) `PolyClobTrade` | condition id, token, side, price, size, venue ts, recv | ~200k/day |
| `Kalshi_{Yes,No}_BBO` | 43 (64 B) `QuoteV3` | bid/ask, **sizes**, recv, wall, seq, asset, changed | keyed by **asset**, not ticker (below) |
| `Kalshi_{Yes,No}_Trade` | 45 (72 B) `TradeV3` | side, price, size, recv (wall), venue ts | |
| `Kalshi_Window` | 48 (80 B) `KalshiWindow` | ticker, asset | `open_unix` is wrong: the ticker's **ET close time read as UTC**, 4 h off and 15 min late; strike is 0. Not used. |
| `PolymarketChainlink_BBO` | 43 (64 B) `QuoteV3` | bid = ask = the Chainlink price, keyed by the *Spot* asset id (BTC 15 … HYPE 21) | from 2026-08-11 |

Asset ids, 15-minute windows: BTC 1, ETH 3, SOL 5, XRP 7, BNB 9, DOGE 11, HYPE 13.

**Sizes are present.** Each BBO record carries the shares at the best bid and best ask,
on both venues. So the tape gives *depth at the touch*, though not beyond it.

## Clocks (`validate.py`, 07-26 and 09-20)

- `recv`: CLOCK_MONOTONIC-like on the recorder host, the same for every stream.
  It drifts about 2.4 s a day against wall time, so it orders records but is not
  a calendar.
- `wall`: CLOCK_REALTIME at receive, on the same host. Kalshi's and Polymarket's
  wall−recv offsets agree to within a few µs **at the same instant**, so one host
  wrote both and **wall time is the timeline every array uses** (`t`).
- Polymarket BBO `pub` sits ~111 s (09-20) or ~43 s (07-26) off `recv`, in some
  other clock plane. It is not a latency and is never used.
- Venue to receive, which says where the recorder sat:

  | | Polymarket trades | Kalshi trades |
  |---|---|---|
  | p1 / p50 | 3.4 / 6.1 ms | 46.7 / 50.1 ms |
  | Dublin, our recorder (`feed_ms`, p50) | ~10 ms | ~47 ms |
  | London, our recorder | ~1 ms | ~38 ms |

  The Helix host is **not London**. Its geometry is close to Dublin's, so its fill
  numbers stand for Dublin with at most a few ms of shift (PLAN 3.1).

## Kalshi's contract switch

The Kalshi BBO stream is keyed by asset. At each roll it keeps the old contract's
terminal book (0 / 0.001) for a few seconds past the open (09-20: p50 2.9 s, p99
7.5 s). Then the new contract's quotes start on a new subscription whose `seq`
restarts at 1. `reader.kalshi_contract_start` finds that restart nearest the
open, and the replay counts the Kalshi leg only from there. This is the tape's
form of the ghost-gap guard. With `entry_early_s ≥ 20` it changes no decision,
but it keeps the leg honest for any earlier variant.

## Validation (PLAN 0.1a)

`helix_validation_<day>.json` under `research/data/`:

| Check | 07-26 | 09-20 |
|---|---|---|
| Array path = `recv3`, field for field (500k records per stream) | pass | pass |
| Polymarket `yes_bid = 1 − no_ask` at each YES tick: exact / within 1¢ | 63% / 89% | 69% / 95% |
| Kalshi YES against NO, exact | 99.9% | 99.7% |
| Ticks inside their window's open–close | 99.0% | 99.7% (none before the open) |
| Book 5 s before close agrees with the tape's resolution (decided books) | 99.4% | 94.5% |
| Polymarket raw ticks/s, 15-minute tokens, per side (deduplicated) | 593 (39) | 1215 (81) |
| Kalshi ticks/s (deduplicated) | 24 (6), 0.8 h only | 939 (206) |

- **Mirror.** YES and NO are separate streams, written a few ms apart. The
  misses are one side updated before the other, not an inversion.
- **Convergence vs settlement.** The tape's own resolution is a strike
  comparison. On 09-20, 3 of 40 windows sampled on Gamma resolved the other way.
  Polymarket has settled on a 60 s TWAP since 2026-08-07, and the strike
  comparison misses that. **Every replay settles on Gamma's outcome**
  (`resolutions.py`), never the tape's. Over all windows the tape agrees with
  Polymarket 100% before the switch and 94.2% after it.
- **Tick rates.** The manual's ~159 ticks/s was across everything at its
  sampling. The raw stream is ~1,200/s per side for the 15-minute tokens alone,
  and 93% of those rows repeat the touch (a deeper level changed). The reader
  drops those (`dedup`), except the first row of each 900 s window.
- **Decode speed.** The array path decodes a Polymarket day (262M records, 105M
  of them 15-minute tokens) in ~40 s on one core, peaking at 2.8 GB. A whole day
  of all three legs takes ~100 s alone and ~4 min with three days in parallel.
  `recv3`'s typed path runs at about 0.5M records/s, which would be ~9 min per
  side-day.

## Days

- Kalshi is missing on 07-21 to 07-25, and 07-26 has 0.8 h of Kalshi. The joint
  span is **07-27 to 10-01**.
- Chainlink starts on 08-11.
- **Settlement regime** (Gamma `resolutionSource`): Chainlink spot stream through
  2026-08-06, the 60 s TWAP stream from **2026-08-07**.
- **Fee:** every window of all 73 days carries `feeSchedule` rate 0.07, exponent 1.
- `research/data/helix_inventory.json` lists each stream and day: records, span,
  checksum, largest gap, and whether the day is used, with the reason.
