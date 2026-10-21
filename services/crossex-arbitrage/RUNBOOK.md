# Runbook

## Keys (Phase 1)

Create one API key per venue with **read permission only**: no trading,
transfers or withdrawals. Restrict each key to the host's static IP.

- Binance: enable only "Enable Reading". The service checks
  `/sapi/v1/account/apiRestrictions` and refuses any other `enable*` or
  `permits*` flag.
- Bybit: create a "Read-Only" key (`/v5/user/query-api` must return
  `readOnly=1`).

Put the keys in `/etc/crossex.env` (root-owned, `0600`) and verify them:

```sh
sudo -u crossex env $(sudo cat /etc/crossex.env | xargs) build/release/crossex check-keys
```

The output must show `"read_only":true` for both venues and the account's
taker fees per symbol.

## Latency bake-off (M0)

Run the same build in Tokyo (`ap-northeast-1`) and Singapore
(`ap-southeast-1`), with keys, for at least 24 h each:

```sh
crossex probe --duration-seconds 86400 > probe-$(hostname)-$(date -u +%F).json
```

Compare `signed_rtt.p99_ms` and `feed_age.p99_ms` for both venues. Choose the
region with the lower worst-venue p99, and record both results in
`docs/aws-tokyo.md`. Expect Tokyo to win on the Binance leg and Singapore on
the Bybit leg; the deciding number is the worse of the two.

## Start / stop

```sh
sudo systemctl enable --now crossex-shadow crossex-compress.timer
curl -s 127.0.0.1:8070/api/summary | jq '{ready,halted,depth_events,gaps,cycles,net_raw}'
journalctl -u crossex-shadow -f
```

The dashboard is at `http://127.0.0.1:8070/` (reach it with an SSH tunnel:
`ssh -L 8070:127.0.0.1:8070 host`).

Allow about 10 minutes after start before any entries. That is the basis
warm-up, and until it ends the risk counter `basis_cold` rises.

## Kill

```sh
touch /data/crossex/KILL      # halt entries, flatten open shadow pairs (latency path)
rm /data/crossex/KILL         # acknowledge; entries resume
```

The kill file is checked every 100 ms and survives restarts. A loss halt
(daily loss, or a pair that couldn't be repaired after 20 attempts) clears
only on restart.

## Gaps

`gaps` in the summary counts book breaks per venue. Every break triggers a
resync: a REST snapshot on Binance, a re-subscribe on Bybit. While it runs,
the symbol takes no entries and its census episodes are closed. A healthy run
shows gaps near zero, and `crossex gapcheck <journal>` reports
`recoveries ≥ gaps`. A gap rate that keeps rising on one venue points at
host network loss or a feed-format change. Capture raw frames with
`--record-raw` on a short manual run and compare them against the parser
tests.

## Disk

Uncompressed, each symbol produces about 1.5 GB/day; zstd brings that to
about 0.35 GB/day. The service restarts daily (`RuntimeMaxSec=1d`), and the
hourly `crossex-compress.timer` compresses every journal except the newest.
Size `/data` for at least 30 days of the basket (15 symbols ≈ 160 GB). The
service stops on a write failure rather than drop data.

## Reports

```sh
crossex census-report /data/crossex/crossex-*.jsonl*   # do the gaps exist?
crossex daily-report  /data/crossex/crossex-*.jsonl*   # P&L + PREREG gate
crossex replay        /data/crossex/<file>             # determinism (same env as the run)
```

## Failure modes

| Symptom | Behaviour | Action |
|---|---|---|
| Exit with `key check failed` | Refuses to start | Use read-only keys |
| `linked libcurl lacks wss://` | Refuses to start | Build against curl ≥ 8.11 (see `docs/aws-tokyo.md`) |
| Feed disconnect | Process exits 1; systemd restarts it after 5 s | Check `journalctl`; repeated failures mean network or venue trouble |
| `recorder queue overflow` | Exits 1 | Disk too slow or full |
| `feed_stale` rising | Entries blocked | One venue's feed is silent; check host network |
| `symbol_disabled` | The symbol's venues disagreed by more than 1% at start | Fix the basket mapping (contract units differ) |

## Binance ↔ Hyperliquid instance

The second instance runs on the same host. It shares the binary and has its
own env, data directory, port and kill file.

```sh
sudo mkdir -p /data/crossex-hl && sudo chown crossex:crossex /data/crossex-hl
sudo install -m 0600 config.hyperliquid.example.env /etc/crossex-hl.env   # add Binance read-only key + CXA_HL_ADDRESS
sudo -u crossex env $(sudo cat /etc/crossex-hl.env | xargs) build/release/crossex check-keys
sudo -u crossex env $(sudo cat /etc/crossex-hl.env | xargs) build/release/crossex probe --duration-seconds 3600
sudo cp ../../infra/systemd/crossex-hl-*.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now crossex-hl-shadow crossex-hl-compress.timer
curl -s 127.0.0.1:8071/api/summary | jq '{peer,ready,depth_events,gaps,cycles,net_raw}'
```

- **Keys.** `check-keys` must show Binance `"read_only":true`, and
  Hyperliquid `address only (no signing key)` with the account's
  taker/maker fees. Never put an HL private or agent key on the host for
  shadow.
- **Probe.** Reads `hyperliquid.feed_age` (bbo `time` versus the
  `exchangeStatus` clock) and `signed_rtt` (the `exchangeStatus` RTT). The
  HL clock offset reads about 200 ms behind wall time from Kigali, because
  `exchangeStatus.time` trails. Compare feed age across runs, not as an
  absolute.
- **Stagger starts.** Start this unit at least a minute after
  `crossex-shadow`. Both pull Binance REST snapshots and `commissionRate`
  at startup from the same IP, and each restarts daily from its own start
  time.
- **Kill.** `touch /data/crossex-hl/KILL` (independent of the Bybit
  instance).
- **Disk.** Binance diffs dominate the volume, at about 0.35 GB per symbol
  per day after zstd. The 21-coin basket adds about 7 GB/day. Grow `/data`,
  or give this instance its own volume, before the 14-day window starts.
- **Reports.** Run them with the HL env loaded:
  `env $(sudo cat /etc/crossex-hl.env | xargs) crossex daily-report /data/crossex-hl/*.jsonl*`.
  Replay refuses a journal whose peer differs from `CXA_PEER`.
- **Thin coins.** A coin whose HL L1 is usually under 2 × $250 (with
  `CXA_AVAILABLE_PPM=500000`) rarely has a valid edge. The census report
  shows it, but don't drop it during the PREREG window.
