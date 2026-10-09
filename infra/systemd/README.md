# systemd units (Linux host: Azure Japan East VM)

Units for running the OFI scalper on the Tokyo VM. They assume:

- user `ofi`, with the checkout at `/home/ofi/trading-algos`;
- profile `dev`;
- recordings on `/data`.

Edit `User=`, the paths and `--profile` in each unit if yours differ.

| Unit | What |
|---|---|
| `ofi-scalper-service.service` | The scalper on `127.0.0.1:8030`: recorder, features, gate and risk |
| `notification-service.service` | Alerts on `127.0.0.1:3010`; the scalper's alerts go through it |
| `ofi-daily-check.service` + `.timer` | 00:20 UTC daily: checks yesterday's recordings and sends a Telegram summary |
| `execution-service-binance.service` | Order entry on `127.0.0.1:8010` (`ADAPTERS=binance_futures`, testnet until approved); the only process with the trading key |
| `ofi-status.service` | Read-only status page for colleagues on `127.0.0.1:8040`, published only through the Cloudflare Tunnel (`infra/cloudflare/README.md`) |
| `execution-service-binance-live.service` | MAINNET gateway on `127.0.0.1:8011` (profile `binance-live`) for micro-live; holds the sub-account key. Not enabled: start by hand |
| `ofi-scalper-live.service` | Micro-live scalper (profile `live`, port 8031, no recording, real money capped in code). Not enabled: start by hand after the steps in the scalper README |
| `ofi-research.service` | One-shot Stage 2 pipeline (`--shallow` by default) at nice 19, idle IO and a memory ceiling, so research runs next to the recorder and the demo bridge. `sudo systemctl start ofi-research`; arguments in a drop-in |
| `ofi-unit-alert@.service` | `OnFailure=` hook on the three services above: a Telegram alert when one fails to start or crashes |

## Host setup (once)

1. **Data disk.** Attach a managed disk, then format it and mount it at `/data`.

   ```sh
   lsblk                                       # find the new, empty disk, e.g. sdc
   sudo mkfs.ext4 /dev/sdc                     # erases that disk: check the name
   sudo mkdir -p /data
   echo "UUID=$(sudo blkid -s UUID -o value /dev/sdc) /data ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
   sudo mount -a && sudo mkdir -p /data/ofi && sudo chown ofi:ofi /data/ofi
   ```

   **Do not use `/mnt`.** On Azure it is the temporary resource disk, which is wiped on deallocate, resize or host maintenance.

2. **Clock.**

   ```sh
   sudo apt install -y chrony
   chronyc tracking
   ```

   Feed latency is measured against Binance's event times, so local clock error shows up in it.

3. **Network.**
   - Use a static public IP and whitelist it on the Binance key.
   - Add **no** inbound rule for 8030 or 3010. Reach the API over SSH: `ssh -L 8030:127.0.0.1:8030 ofi@<vm>`.

4. **Python environment.** Install uv, then sync on Python 3.12, the version CI uses:

   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh
   cd ~/trading-algos && uv sync --python 3.12 --package ofi-scalper-service --group dev
   ```

5. **Config.**
   - Copy `services/ofi-scalper-service/.env.example.dev` to `.env.dev`, fill it in and `chmod 600` it.
   - Keep `OFI_RECORD_DIR=/data/ofi/raw/dev`.
   - Configure `services/notification-service/.env` from its `.env.example`.

6. **notification-service build.**

   ```sh
   sudo apt install -y nodejs npm
   cd services/notification-service && npm ci && npm run build
   ```

## Install

```sh
cd ~/trading-algos/infra/systemd
sudo cp notification-service.service ofi-scalper-service.service ofi-unit-alert@.service \
        ofi-daily-check.service ofi-daily-check.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now notification-service ofi-scalper-service ofi-daily-check.timer
```

Never start the service with `sudo .venv/bin/ofi-scalper-service`. Files it creates would then belong to root, and the unit, which runs as `ofi`, could no longer write its logs.

## Order entry (testnet)

1. Create **demo-trading** API keys (Binance demo trading, not the main site).
   On the demo account set one-way position mode, single-asset margin,
   leverage ≤ 2 and isolated margin for BTCUSDT and ETHUSDT. The service
   checks these at startup and stays not-ready, with the exact fix, until they hold.
2. `cp services/execution-service/.env.example.binance services/execution-service/.env.binance`,
   fill it in and run `chmod 600` on it.
3. Install `execution-service-binance.service` like the others, then check:

   ```sh
   curl -s localhost:8010/health/ready | python3 -m json.tool          # preflight, user stream
   curl -s localhost:8010/health/trading-ready | python3 -m json.tool  # gates
   ```

Kill controls on the gateway (they work even with `TRADING_ENABLED=false`):
`POST /v1/accounts/binance_testnet/cancel-all`, `/flatten`, and `/dead-man`
(`{"countdown_ms": 15000}` to arm, `0` to disarm).

## Failure alerts

A service that cannot start sends no alert of its own: on 2026-10-02 the scalper
crash-looped for an hour on a config error without anyone being told. Each
service unit therefore has `OnFailure=ofi-unit-alert@%n.service`, which runs
`unit_alert.py` when the unit fails:

- **Channels:** notification-service first; if that fails, straight to Telegram with
  the scalper's own bot (`OFI_TELEGRAM_BOT_TOKEN` to `OFI_TELEGRAM_ADMIN_USER_IDS`).
  Both are read from `services/ofi-scalper-service/.env.dev`, leniently, so a broken
  value elsewhere in that file does not stop the alert.
- **Robust:** it runs the system `/usr/bin/python3` on a standard-library-only
  script, so a broken venv or dependency is reported too.
- **Rate limit:** one alert per unit per 30 minutes; the next says how many failures
  were skipped. Each restart that succeeds sends the usual "started" alert.

Install or update (after `git pull`), then check the channel works:

```sh
cd ~/trading-algos/infra/systemd
sudo cp ofi-unit-alert@.service ofi-scalper-service.service \
        execution-service-binance.service notification-service.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start ofi-unit-alert@alert-test.service       # a test alert should arrive
journalctl -u ofi-unit-alert@alert-test.service -n 5 --no-pager   # "alert sent via ..."
sudo rm -f /var/lib/ofi-unit-alert/alert-test.json
```

The services pick up `OnFailure=` from the reloaded units without a restart. It
relies on systemd 254 or later (Ubuntu 24.04 has 255), where a unit that
`Restart=always` restarts still passes through its failed state, which is what
triggers `OnFailure=`. `systemctl --version` shows yours.

## Operate

```sh
systemctl status ofi-scalper-service
journalctl -u ofi-scalper-service -f                     # includes the once-a-minute ofi_heartbeat line
journalctl -u ofi-scalper-service | grep ofi_heartbeat | tail -5
curl -s localhost:8030/health/ready | python3 -m json.tool
systemctl list-timers ofi-daily-check.timer
journalctl -u ofi-daily-check                            # yesterday's summary and exit status
sudo systemctl start ofi-daily-check                     # run the check now
```

**Kill switch:**
- Halt: `touch ~/trading-algos/services/ofi-scalper-service/data/KILL.dev`, `POST /v1/kill`, or `/kill` to the scalper's Telegram bot.
- Resume: remove the file, then call `POST /v1/kill/ack`.

**Upgrades:**

```sh
git pull
uv sync --python 3.12 --package ofi-scalper-service
sudo systemctl restart ofi-scalper-service
```

A restart costs a few seconds of recording. The recorder appends to the current hour's file, and the books resync from fresh snapshots.

## When the recording volume fills

When free space drops below `OFI_MIN_FREE_DISK_GB` (default 10), the recorder:

1. closes its files;
2. stops writing;
3. sends a Telegram alert.

`/v1/status` → `recorder.disk_paused` is true while this lasts. Recording resumes on its own once free space is back above 120% of the floor.

The service, features and risk keep running throughout. Only the recording stops.

## poly-kalshi (London VM)

`poly-kalshi-service.service`, `pk-status.service` and `pk-unit-alert@.service` run the
Kalshi/Polymarket convergence recorder and paper trader (`services/poly-kalshi-service`).
The host setup is the same as above, with these differences.

- **Region:** London (Polymarket's CLOB is in AWS eu-west-2). The UK is close-only on Polymarket. This host records and paper-trades, but it can never place a real order.
- **User and data:** user `pk`. Run `sudo mkdir -p /data/pk && sudo chown pk:pk /data/pk`, and set `PK_RECORD_DIR=/data/pk/raw/dev`. Size the disk for several GB/day of compressed tape.
- **Build:** the service is Rust (a Cargo workspace). Install a stable toolchain with rustup as `pk`; the units run `services/poly-kalshi-service/target/release/*`.
- **Kalshi key:** put the PEM where `KALSHI_PRIVATE_KEY_PATH` points, owned by `pk`, `chmod 600`.
- **Ports:** no inbound rule for 8050 or 8051. Use `ssh -L 8050:127.0.0.1:8050 -L 8051:127.0.0.1:8051 pk@<vm>`.

```sh
cd ~/trading-algos/services/poly-kalshi-service && cargo build --release --workspace
cd ~/trading-algos
sudo cp infra/systemd/poly-kalshi-service.service infra/systemd/pk-status.service \
        'infra/systemd/pk-unit-alert@.service' /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now poly-kalshi-service pk-status
curl -s 127.0.0.1:8050/health/ready                      # both feeds up, all windows identified
```

- **Kill switch:** `touch services/poly-kalshi-service/data/KILL.dev`, `POST /v1/kill`, or `/kill` to the bot. These halt new paper entries only; recording continues.
- **Read the gates:** `target/release/pk readout --journal data/state/dev/journal`.
- **Restarts:** each restart starts a new hour file (`pk_<day>_<hour>_<session>.zst`). It never appends to one a crash may have left truncated. The research commands still read the older `.gz` hours.
- **Upgrades:** `git pull && cargo build --release --workspace && sudo systemctl restart poly-kalshi-service pk-status`. The running binaries are replaced on disk; the restart picks them up.
- **Order-path probe:** `target/release/pk probe-orders` with `PK_ORDER_PROBE=on` and a funded `POLY_PRIVATE_KEY` (README, "Order path"). It posts post-only orders below the bid and cancels them at once; it never fills.

## poly-kalshi on the Dublin host (`polymarket`, Azure North Europe)

The poly-kalshi units here assume user `pk` and a `/data` disk. The Dublin host
runs them as `poly` from `/home/poly/trading-algos`, recording to the OS disk.
They were installed with:

```sh
for u in poly-kalshi-service.service pk-status.service pk-unit-alert@.service; do
  sed -e 's/^User=pk$/User=poly/' -e 's/^Group=pk$/Group=poly/' \
      -e 's#/home/pk/#/home/poly/#g' -e '/^RequiresMountsFor=\/data$/d' "$u" \
    | sudo tee /etc/systemd/system/"$u" >/dev/null
done
sudo systemctl daemon-reload && sudo systemctl enable --now poly-kalshi-service pk-status
```

Before this (until 2026-10-07) they were started by hand, and a reboot left the
tunnel (`poly.nishimweprince.dev` → `127.0.0.1:8051`) answering 502.

## Moving poly-kalshi to AWS eu-west-1 (Dublin)

Polymarket's CLOB origin is AWS eu-west-2 (London), behind Cloudflare. eu-west-1 is
the nearest region Polymarket allows to trade from (the UK is close-only). The Azure
North Europe host is also in Dublin, so the question is measured, not assumed:
**is the AWS host's Polymarket feed lag and CLOB round trip better by enough?**

1. **Instance.** Ubuntu 24.04, ≥ 4 vCPU on a compute-optimised type (c7i.xlarge or
   c7g.xlarge), an EBS gp3 data volume of ≥ 200 GB mounted at `/data` (`/etc/fstab`
   with `nofail`), chrony on (`sudo apt install chrony`; Amazon Time Sync is at
   169.254.169.123 and is the default on the AWS images). Security group: inbound SSH
   only. No 8050/8051/8052.
2. **User, checkout, units.** As in "poly-kalshi (London VM)" above (user `pk`,
   `/data/pk`). Copy `.env.dev` from Dublin, then change:
   - `PK_HOST_TAG=aws-euw1`
   - `PK_RECORD_DIR=/data/pk/raw/dev`
   - **`PK_EXECUTION_MODE=off`** for the comparison (records, decides, observes; no
     paper orders, no ledger change)
   - **remove `PK_TELEGRAM_BOT_TOKEN`** until the cutover: two hosts polling one bot
     token steal each other's updates.
   - Same `PK_RESULTS_EPOCH` for now.
3. **24 h side by side.** Both hosts run. On each:
   `pk netprobe --minutes 1440` (warm HTTP/2 round trip to the CLOB and Kalshi),
   and the live feed lags on the status page or `/v1/obs/status` → `engine.latency`:
   `feed_ms_p` and `feed_ms_k` (receive time minus the venue's stamp; both hosts run
   chrony against nearby sources, so the skew is small and similar), plus
   `observer.behind_ms` and the trader's `loop_lag_ms`.
4. **Decide** (PLAN 5.1): move if AWS improves `feed_ms_p` p50 by > 2 ms or p99 by > 10 ms,
   or the CLOB round trip p50 by > 2 ms. Otherwise stay on Azure and stop the AWS host.
5. **Cutover**, if it moves, at a window roll:
   - On Azure: `sudo systemctl stop poly-kalshi-service pk-status`. Copy
     `data/state/dev/` (journal and ledger) to the AWS host, so the page keeps the history.
   - On AWS: `PK_EXECUTION_MODE=paper`, `PK_RESULTS_EPOCH=<n+1>` (fills depend on latency;
     PREREG part 4 counts the epochs separately), restore `PK_TELEGRAM_BOT_TOKEN`, then
     `sudo systemctl restart poly-kalshi-service pk-status`.
   - Move the Cloudflare tunnel (`poly.nishimweprince.dev` → `127.0.0.1:8051`) to the AWS
     host (infra/cloudflare/README.md).
   - Leave the Azure host recording with `PK_EXECUTION_MODE=off` for a week if the disk
     allows: two tapes of the same windows from two hosts measure the latency's effect on
     the same decisions.
