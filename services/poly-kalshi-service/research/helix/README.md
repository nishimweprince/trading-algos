# The Helix kpc tape

`~/kpc/raw` (`HELIX_TAPE_DIR`): 73 days of 15-minute up/down markets on both venues,
recorded by Helix's recorder, 2026-07-21 to 2026-10-01 (Kalshi from 07-13, with a
gap on 07-21 to 07-25). About 368 GB, zstd-compressed. **It is read in a stream and
never copied:** everything here writes derived outputs only.

The code is Rust, in `crates/pk-research/src/helix/` (ported from the Python
`recv3.py` and `reader.py`, which were Helix's own decoder and our wrapper of it):

| Module | |
|---|---|
| `container.rs` | The `.hlxz` block container and the older HLXF frame stream. |
| `records.rs` | Typed records (windows, resolutions, BBO, quotes, trades, books). |
| `series.rs` | The array path: top of book per key, deduplicated, and the Kalshi contract switch. |
| `validate.rs` | PLAN 0.1a's checks on a day: `pk helix validate --day 20260920` |
| `resolutions.rs`, `gamma.rs` | Each window's settlement from the venues (Gamma, Kalshi), cached. |
| `replay.rs`, `sweeps.rs`, `leadlag.rs`, `fairvalue.rs`, `inventory.rs` | The studies (`pk helix …`). |

The tests run every module on a small synthetic tape that carries the real
tape's quirks (restarts, legacy and wide tags, stale-width and damaged blocks, a
partial tail, an HLXF file), against the Python modules' outputs
(`crates/pk-research/tests/golden/helix/`). Below, `recv3` and `reader` name the
Python decoder the measurements were taken with.

## Container

A day file is `<Stream>/<YYYYMMDD>.hlxz`: a 32 B `FileHeader` (magic `HLX1`,
version 1), then blocks of a 40 B `BlockHeader` (magic `HLXB`, type tag, record
size, count, compressed and raw length, first and last ts), each followed by one zstd
frame of same-tag, same-size records. A recorder restart appends a new FileHeader
to the same file (07-21: 8 sub-files), so the reader walks blocks inline and
resyncs at each header. A footer index is used only for single-sub-file days,
as in `recv3`, and only by the inventory: a day rolled off at midnight gets its
footer a second or two late, so decoding never needs it. Records sit in **block
order, not time order**: the arrays are sorted on read.

**Record sizes come from the block header.** Payloads are frozen-append, so a
tag's record can grow (`OrderEvent` went 56 → 64 B on 2026-08-03). Each reader
names the smallest size it decodes (the table below), takes every block at least
that wide, and splits it by the block's own size. A narrower block (an older
layout) is skipped; when *every* block of a wanted tag is narrower, the read
fails. An empty result from a populated tag is a reader bug, never a dead stream.

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
| `Kalshi_Window` | 34/48 (80 B) `KalshiWindow` | recorded ts, ticker, strike, asset | `open_unix` is wrong: the ticker's **ET close time read as UTC**, 4 h off and 15 min late; strike is 0. `records::iter_kalshi_windows` decodes it and `kalshi_ticker_at(asset, t)` gives the ticker in force from the recorded time, not `open_unix`. The studies do not use it. |
| `PolymarketChainlink_BBO` | 43 (64 B) `QuoteV3` | bid = ask = the Chainlink price, keyed by the *Spot* asset id (BTC 15 … HYPE 21) | from 2026-08-11 |

Asset ids, 15-minute windows: BTC 1, ETH 3, SOL 5, XRP 7, BNB 9, DOGE 11, HYPE 13.
The even ids are the 5-minute slots, and `Polymarket_Window` carries both, so
`iter_windows` keeps a window only if its asset is a 15m slot **and**
`open_unix % 900 == 0`.

Decoding rules, in `records.rs`:

- **Asset id by tag.** Under a wide tag (43–48) the u16 `asset_wide` is
  authoritative, and under the older tags the u8 (`asset_of`). For ids 1–21 both
  agree on the tape. A wide record whose `asset_wide` is still zero while the u8
  is set falls back to the u8. Asset ids are u16 throughout.
- **Condition ids are normalized** (`norm_condition_id`: no `0x`, lowercase).
  `PolyWindow`/resolution carry bare hex and BBO/trades `0x` hex, so a raw join
  returns nothing.
- **Polymarket BBO and trades carry no asset.** Their only identity is the
  token. `token_index(windows)` maps `token_id → (asset, open_unix, outcome)`.
  The trade record's field is *named* `asset_id` but holds the token id
  (`PolyTrade::token_id`).
- **Kalshi trade `side` is the YES/NO leg, not the aggressor.** Classify the
  aggressor against the prevailing BBO with `records::aggressor` (the quote
  rule).
- **Resolution `outcome_ws`** is decoded as `Option` (255 → `None`, which is
  frequent). Never settle on it (below).
- **Venues** (`records::venue`): Kalshi YES/NO 31/32, Polymarket YES/NO 25/26.
  Venue 8 (`PolymarketClob`) is order routing only, and no feed ever writes it.
  `records::SOURCE_IDS` lists each stream's `FileHeader.source_id`.

**Sizes are present.** Each BBO record carries the shares at the best bid and best ask,
on both venues. So the tape gives *depth at the touch*, though not beyond it.

## Clocks (`validate.py`, 07-26 and 09-20)

The format write-up (2026-10-06) names Polymarket BBO offset 0 a wall clock and
Kalshi trade offsets 48/56 receive-monotonic/wall. The measurements below show
offset 0 moving with the Kalshi monotonic `recv` (same wall − recv offset at the
same instant) and Kalshi trade 56 sitting ~50 ms before 48 (a venue time). The
decoder keeps the measured meanings, which every golden depends on.

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
