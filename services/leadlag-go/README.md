# leadlag-go

BTC/ETH order flow → alt USDⓈ-M perpetual lead–lag, in Go. The plan is shadow only: no demo or live orders.

The original strategy:
1. Watch taker flow in the two leaders (BTCUSDT, ETHUSDT).
2. When a burst of one-sided flow appears, look at the alts that tend to follow.
3. Take the follower in the same direction, as a taker, if its expected move clears the round-trip taker fee.

explore-1 and explore-2 showed that the response is real (+0.6 to +4 bp) but far below a VIP 0 taker round trip
(~10 bp). explore-3 (PREREG Part 4) therefore tests the signal as a **maker** tool on the **USDC-margined**
followers, which are reported to have 0 bp maker fees:
- a signal-gated post-only quoter (arm A);
- a two-sided quoter that pulls the side a leader move would run over (arm B);
- a large-move taker entry with a β-gap-closure exit (3c);
- explore-2 with a gap-closure exit (3d).

The research runs on Binance's free aggTrades archive (data.binance.vision). Live shadow trading runs
on an AWS EC2 instance in Tokyo. There, mainnet feeds produce signals, and simulated fills against the
mainnet book produce P&L. A separate, read-only stats page is published through cloudflared
(`deploy/DEPLOY.md`).

## Status

| Milestone | Scope | State |
|---|---|---|
| M1 | Archive fetch, verify and pack (`llfetch`), deterministic multi-symbol merger | done |
| M2 | Live ingest + recorder, observer, status page, `llprobe`, EC2 deploy kit | built; running on the ofi VM (shared host) while the AWS host is down |
| M3 | Flow features, screen, event study, placebo → gate G1 | — |
| M4 | Policy, fill model, backtest, prereg freeze, validation, held-out → G2–G4 | — |
| M5 | Live shadow with the frozen parameters → G5 | engine built. `explore-1` (flow trigger, 1 s taker exit) is finished: the response was real but small, and it lost after fees (PREREG Part 3). **Exploratory** run `explore-2` is live (price-move trigger, top-3 lagging followers, 30 s maker exit, markouts). **explore-3a–3d** (PREREG Part 4: USDC-M passive quoter with a queue model, β-gap exits, liquidation filter) are built and run next to it once `llprobe fees` confirms the USDC-M rates. None counts toward a gate; G5 starts at the M4 freeze |

## Layout

```
cmd/leadlag            trader: feeds → hub (tape ring → parse → engine); admin :8060; --replay for parity
cmd/leadlag-observer   tape writer + feed statistics; unix socket in, loopback :8061
cmd/leadlag-status     read-only page :8062 (the only thing behind cloudflared)
cmd/llprobe            latency probe: REST RTT, cold connects, feed lag, aggTrade publish delay; `fees`: commissionRate
cmd/llfetch            archive downloader: plan | fetch | verify | candidates [--quote USDC] | ticks
cmd/llresearch         M3 study; `shadow`: a shadow run's net CI and PREREG Part 4 kill criteria
internal/live          Hub (one lock; dedupe; digest), record engine, tape replay
internal/shadow        shadow engines: leader flow or price-move events → simulated fills → journal. Taker entry with taker,
                       maker or β-gap exits; or the quoter (post-only orders, conservative queue model, arms A/B)
internal/ws            reconnecting combined-stream client (receive stamp at frame completion)
internal/parse         zero-allocation aggTrade / bookTicker / forceOrder decoder
internal/ipc           SPSC byte ring + record protocol + producer (never blocks the hot path)
internal/observe       observer core: tape, aggregates, disk guard
internal/tape          hourly .jsonl.zst tape + manifest; reader
internal/stats         allocation-free log-linear histograms
internal/model         Event (E8 fixed-point prices and quantities)
internal/fixed         allocation-free decimal parsing
internal/archive       data.binance.vision client, sha256 check, CSV → .llp packer, day manifests
internal/source        Source interface; Archive merges symbols by (TradeMs, Sym, AggID)
internal/symbols       exchangeInfo: tick/step sizes, perpetual list
```

## Archive

```sh
go build -o bin/ ./cmd/...
export LL_ARCHIVE_ROOT=/data/leadlag/archive

bin/llfetch candidates --n 30                   # top USDT perps by 24 h volume, listed before 2025
bin/llfetch plan   --symbols BTCUSDT,ETHUSDT --from 2025-10-01 --to 2026-01-31   # size estimate
bin/llfetch fetch  --symbols BTCUSDT,ETHUSDT --from 2025-10-01 --to 2026-01-31 --concurrency 4
bin/llfetch verify --symbols BTCUSDT,ETHUSDT --from 2025-10-01 --to 2026-01-31
```

- **Files.** Whole past months come from the monthly zip. If a monthly zip isn't published, `fetch`
  falls back to the daily ones. Partial months and the current month use daily zips.
- **Checksums.** Every zip is checked against its `.CHECKSUM` file. A mismatch is retried, and is never
  packed.
- **Resuming.** Packing is idempotent: days that are already packed are skipped, so an interrupted
  `fetch` resumes where it stopped.
- **Disk layout.** `root/llp/<SYM>/<SYM>-<day>.llp` holds the trades, in zstd-compressed 32-byte
  records. A `.json` manifest sits next to each file with the row count, first and last ids and
  times, missing aggregate-trade ids, and the source zip's sha256.
- **What `verify` checks:**
  - every day is present and readable;
  - id gaps inside a day, and seams where a day's first id isn't the previous day's last id + 1;
  - timestamps that go backwards.

  Gate G0 excludes gap days from research.
- **Size.** A month of BTCUSDT is about 0.45–0.7 GB zipped. Packed `.llp` files are about 0.75× their
  zips. Run `plan` first.

Measured on the Mac, 2026-10-05: SOLUSDT has 268,975 trades and ETHUSDT 889,112; neither has gaps.
Download speed from the Mac is about 0.3 MB/s, so bulk fetches belong on the EC2 host.

## Live recording (M2)

Three processes, with stats kept off the trading loop:

1. **`leadlag`** (the trader) copies each frame into a 64 MB ring and parses it. The engine runs under
   one lock.
2. **`leadlag-observer`** drains the ring over a unix socket, writes the hourly tape and keeps the
   statistics.
3. **`leadlag-status`** serves a cached page.

The trader never writes to disk and never serves a viewer. `deploy/DEPLOY.md` is the AWS Tokyo runbook:
AZ probe, host setup, systemd units with CPU pinning, push/rollback, cloudflared + Access, and
acceptance checks.

**Parity.** The hub folds every dispatched event into a digest. `leadlag --replay <tape dir>` re-runs
the tape through the same hub, so the digest a run logs at shutdown must equal its replay's digest.
`internal/live/e2e_test.go` checks this end to end through the socket and observer.

On the Mac it passed against mainnet: 91,681 events, digest `39e1aa7be89e60aa` both live and replayed.

**Measured on the Mac, 2026-10-07** (Mac numbers don't count toward gates):
- Parse: about 650 ns per frame, 0 allocations.
- Engine p50: 2–5 µs.
- Tape: about 18 KB/s for 7 symbols.

**Finding: aggTrade is not real time.**
- An aggregate trade is published late. On BTC/ETH, `E − T` has p90 ≈ 150 ms, and `E` equals the next
  trade's `T` in about 60% of cases.
- bookTicker has `E == T`.

So the archive (which has only `T`) must treat a trade as observable after the measured publish delay,
not at `T`. The live leader signal may need bookTicker. The recorder measures both per symbol (status
page, "agg E−T" and "E = next T"); M3 freezes them in PREREG.

## Development

```sh
go vet ./... && go test -race ./...
go run honnef.co/go/tools/cmd/staticcheck@v0.8.1 ./...
```

Checks (no GitHub Actions CI; run them locally): `go vet ./...`,
`go run honnef.co/go/tools/cmd/staticcheck@v0.8.1 ./...`, `go test -race ./...`, a one-pass
benchmark run (`go test -run '^$' -bench . -benchtime 1x ./...`) and a linux/amd64 build.
