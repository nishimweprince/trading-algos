# leadlag-go

BTC/ETH order flow → alt USDⓈ-M perpetual lead–lag, in Go. The plan is shadow only: no demo or live orders.

The strategy works like this:
1. Watch taker flow in the two leaders (BTCUSDT, ETHUSDT).
2. When a burst of one-sided flow appears, look at the alts that tend to follow.
3. Take the follower in the same direction, as a taker, if its expected move clears the round-trip taker fee.

The research runs on Binance's free aggTrades archive (data.binance.vision). Live shadow trading runs
on an AWS EC2 instance in Tokyo. There, mainnet feeds produce signals, and simulated fills against the
mainnet book produce P&L. A separate, read-only stats page is published through cloudflared
(`deploy/DEPLOY.md`).

## Status

| Milestone | Scope | State |
|---|---|---|
| M1 | Archive fetch, verify and pack (`llfetch`), deterministic multi-symbol merger | done |
| M2 | Live ingest + recorder, observer, status page, `llprobe`, EC2 deploy | — |
| M3 | Flow features, screen, event study, placebo → gate G1 | — |
| M4 | Policy, fill model, backtest, prereg freeze, validation, held-out → G2–G4 | — |
| M5 | Live shadow with the frozen parameters → G5 | — |

## Layout

```
cmd/llfetch            archive downloader: plan | fetch | verify | candidates
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

## Development

```sh
go vet ./... && go test -race ./...
go run honnef.co/go/tools/cmd/staticcheck@v0.8.1 ./...
```

CI: `.github/workflows/leadlag-go-ci.yml` runs vet, staticcheck, race tests, a one-pass benchmark
run and a linux/amd64 build.
