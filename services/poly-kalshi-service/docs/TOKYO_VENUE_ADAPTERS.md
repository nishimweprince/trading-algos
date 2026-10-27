# Tokyo venue-adapter runbook

`pk-predict` and `pk-hip4` are standalone processes. Each opens its own read-only Kalshi feed, venue feeds, journal, risk ledger, kill file, and loopback API. They are not installed on an existing production host and are not in the `pk-trader`, `pk-engine`, or `pk-core` dependency graph.

## Host

Provision Ubuntu 24.04 in AWS `ap-northeast-1` with a current compute-optimized instance offering at least four vCPUs and 8 GiB RAM. Predict identifies Tokyo as its primary region; Hyperliquid's low-latency Foundation node is in `apne1-az1`. The public Hyperliquid API is the v1 transport. A local non-validating node and order-book server are a later optimization after measurement, not a v1 dependency.

Do not expose 8056 or 8057 publicly. Both listeners bind to `127.0.0.1`; access status through SSH forwarding. Restrict security-group ingress to SSH from the operator network.

## Install

1. Create the unprivileged `pk` user, clone into `/home/pk/trading-algos`, and install the Rust 1.88 toolchain.
2. Install `chrony`, enable it, and require `chronyc waitsync 10 0.1` to pass. This enforces the 100 ms clock-offset readiness limit before either service starts.
3. Build with `cargo build --release -p pk-predict -p pk-hip4 -p pk-status` from `services/poly-kalshi-service`.
4. Copy `.env.example.tokyo` to `.env.tokyo`, fill values directly on the host, and run `chmod 0600 .env.tokyo`. Never paste keys into a command line, journal, support bundle, or status response.
5. Register a dedicated Hyperliquid API wallet in the Hyperliquid UI. Put that agent key in the env file and keep the master key out of the VM.
6. Copy `infra/systemd/pk-predict.service`, `pk-hip4.service`, and `pk-unit-alert-tokyo@.service` to `/etc/systemd/system/` (the Tokyo alert template reads `.env.tokyo`; the shared template points at `.env.dev`, which does not exist on this host). A failing adapter triggers `pk-unit-alert-tokyo@<unit>.service`, which sends a Telegram alert via `pk-unit-alert`.
7. Run `systemctl daemon-reload`, but do not enable live mode.

## Predict market discovery

Live Predict markets carry no `kalshiMarketTicker` (always `null`) and 15m markets resolve via `CHAINLINK`, not `PYTH`. Discovery therefore pages `GET /v1/markets?status=OPEN` by cursor, keeps `*-updown-15m` series for the configured assets, derives the window open from `categorySlug` (`<series>-<open_unix>`, close = open + 900s), and resolves the Kalshi contract by querying series `KX<ASSET>15M` for the market with matching open/close. An explicit venue ticker, when present, still takes precedence. Anything unresolved fails closed to grade `E2`.

## Testnets

Protocol tests run against testnets before mainnet shadow soak. Neither venue needs mainnet credentials for these steps.

Predict (BNB testnet, chain 97):

```text
PREDICT_CHAIN_ID=97 \
PREDICT_API_URL=https://api-testnet.predict.fun \
PREDICT_BNB_RPC_URL=https://bsc-testnet-rpc.publicnode.com \
  ./target/release/pk-predict --profile tokyo preflight
```

The WebSocket base is derived from `PREDICT_API_URL`, so the testnet API URL selects the testnet socket automatically. Live mode refuses chain 97; shadow mode is the testnet mode. `approve` and `probe-orders` work the same way.

Hyperliquid: override `HL_API_URL`/`HL_WS_URL` to the testnet endpoints and run `pk-hip4 preflight` / `probe-orders`. The agent wallet must be registered on testnet too (same one-time UI step, on the testnet app).

## Allowlist entries (HIP-4)

`pk-hip4 --profile tokyo build-allowlist [--write]` matches every upcoming HIP-4
`binaryPrice` contract to the Kalshi "above" contract (`KXBTCD`, `KXETHD`,
`KXINXU`, `KXNASDAQ100U`, `KXGOLDD`, …) with the same underlying, strike
(Kalshi's `79,999.99 greater` or `80,000 greater_or_equal` is HIP-4's `> 80000`)
and decision time, and writes both rule fingerprints, a 0.02 basis for the
different settlement index, maker only, expiring at the decision time.
`pk-hip4-allowlist.timer` runs it every 30 minutes into
`data/hip4-allowlist.json` (`PK_HIP4_MATCH_ALLOWLIST`), since Kalshi lists its
hourly contracts only a day or two ahead. `config/hip4-allowlist.json` stays the
empty template. To add an entry by hand instead:

1. Run shadow discovery and find the candidate in `/v1/hip4/markets` or the journal's `match` records: HIP-4 outcome ID, Kalshi ticker, and both rule fingerprints.
2. Add an entry with the venue, outcome ID, ticker, both fingerprints, side mapping, maximum basis adjustment, lane flags, and the basis reason. Discovery reloads the file every 60 seconds; no restart is needed.
3. Confirm the next `match` record flips the grade from `E2` to `E1` with the expected executable lanes.

Any rule change on either side invalidates the entry automatically (the fingerprints stop matching). Expired entries fail closed to `E2`.

## Units (Tokyo host, 2026-10-26)

Installed from `infra/systemd` with `User=ec2-user` and `/home/ec2-user` paths
(the host has no `pk` user): `pk-predict`, `pk-hip4`, `pk-hip4-allowlist.timer`,
`pk-unit-alert-tokyo@`. Both adapters run `shadow` (simulated fills). Raw frame
recording is off (`PK_PRED_RAW`, `PK_HIP4_RAW`); on, it wrote 3 GB in 90 minutes.

## Settlement (Predict)

The background settlement task journals `settle_candidates` every 30 seconds: WON legs ready to redeem and balanced YES/NO pairs ready to merge. Execution is off by default; enable per action in live mode only:

- `PK_PRED_AUTO_REDEEM=yes` redeems WON legs via the position contract or the neg-risk adapter and clears that market's local inventory.
- `PK_PRED_AUTO_MERGE=yes` merges exactly balanced pairs back into USDT.

Receipts land in the journal as `redeemed` / `merged`; failures as `settle_error` without touching risk state. Candidates on unknown markets or with unparseable IDs are skipped, never guessed.

## Markouts and series

Every fill (live or shadow) is watched for markouts at 5, 30, and 60 seconds against the same-side venue mid. Finished markouts appear in the status payload's `markouts` array and in the journal as `markout` records. One series point per market per second (`[t_ms, kalshi_fair, venue_mid]`) is available at `/v1/<venue>/series?since=`. The maker/taker promotion gates read these: non-negative fee- and basis-adjusted markouts at all three horizons before maker live, positive net taker markouts before taker live.

## Preflight and probes

Run as `pk`:

```text
./target/release/pk-predict --profile tokyo preflight
./target/release/pk-hip4 --profile tokyo preflight
```

`pk-predict approve --confirm` first verifies the configured BNB chain (56 mainnet, 97 testnet) and that the Privy signer owns the smart account's ECDSA validator, then submits only missing smart-account allowances and prints every transaction receipt. Order probes are state-changing and require `--confirm`:

```text
./target/release/pk-predict --profile tokyo probe-orders --confirm
./target/release/pk-hip4 --profile tokyo probe-orders --confirm
```

## Shadow soak and promotion

Enable both units only in `shadow`. Check `/health/ready`, `/predict`, and `/hip4` over an SSH tunnel. Readiness is lost and maker orders are cancelled on stale books, a Kalshi sequence gap, rule-fingerprint change, reconciliation drift, clock failure, risk latch, kill file, or failed emergency cancellation.

Maker live requires seven continuous mainnet-shadow days, 300 actionable matches, clean reconciliation, no feed gaps/queue overflow, and non-negative fee/basis-adjusted markouts at 5/30/60 seconds. Then set mode, explicit capital caps, `LIVE_CONFIRM=yes`, and `MAKER_LIVE_CONFIRM=yes`; leave taker confirmation `no`. Taker promotion separately requires 300 shadow taker opportunities, positive latency/depth/slippage-adjusted markouts, and `TAKER_LIVE_CONFIRM=yes`.

## Operations

- Predict kill file: `data/KILL.predict`; HIP-4 kill file: `data/KILL.hip4`.
- Stop or SIGTERM initiates cancel-all. Predict first removes orders off-book, then batches each exact order struct to the appropriate standard/yield/negative-risk exchange's on-chain `cancelOrders`; 20-second signed expirations remain a second replay bound. HIP-4 uses cancel-by-cloid and arms `scheduleCancel` when the account is eligible.
- Predict requotes one market at a time: they remove that market's old bids off-book by the order ids we tracked (one request, retried up to 3 times before latching), then submit the new bids together. An open order we hold no id for (a submit whose answer was lost) is removed once two orphan sweeps 15 s apart saw it untracked (`orphans_removed` in the journal).
- `POST /v1/<venue>/kill` latches and cancels; acknowledgement is separate at `/kill/ack`.
- `flatten` refuses to cross beyond the configured slippage cap and reports remaining inventory.
- Journals and raw tapes are separate under `data/state-predict` and `data/state-hip4`.
- A systemd failure triggers `pk-unit-alert@…`; inspect with `journalctl -u pk-predict -u pk-hip4`.

Before each deployment, verify `cargo tree -p pk-trader` is unchanged and run the existing trader replay benchmarks. Starting these units must never be added to an existing host's target or to the existing trader/status readiness dependency chain.
