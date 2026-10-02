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
sudo cp notification-service.service ofi-scalper-service.service \
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
