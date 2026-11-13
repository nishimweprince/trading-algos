# Tickmind AI: signal watcher + signal backtest

## What the site exposes

| Endpoint | Auth | Contents |
|---|---|---|
| `GET /api/signals` | **none** | all signals since 2026-05-01: id, side, entry, stop, target, status, result (pips), `postedAtUnix` (exact UTC post time). Active signals come back with levels = 0 |
| `GET /api/performance?start=&end=` | none | same list + win rate / net pips for a range; stop/target = 0 |
| home page `/` (signed in) | login | active signal card with ENTRY / STOP / TARGET |
| `/api/push/subscribe` | login | browser web-push (not usable headless) |

So for **detection**, a plain HTTP poll of `/api/signals` is enough. For **live levels**
of an active signal you need the signed-in page, which is what Playwright is for.

## Watcher on Ubuntu

```bash
sudo apt-get update && sudo apt-get install -y python3-venv
cd ~/tickmind && python3 -m venv .venv && . .venv/bin/activate
pip install playwright && playwright install --with-deps chromium
cp tickmind.env.example tickmind.env && chmod 600 tickmind.env   # fill in
set -a; . ./tickmind.env; set +a; python watch_signals.py --once  # test
```

`tickmind.env.example`:

```
TICKMIND_EMAIL=
TICKMIND_PASSWORD=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
# NTFY_URL=https://ntfy.sh/some-long-random-topic
POLL_SECONDS=30
```

systemd (`/etc/systemd/system/tickmind-watch.service`):

```ini
[Unit]
Description=Tickmind signal watcher
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/tickmind
EnvironmentFile=/home/ubuntu/tickmind/tickmind.env
ExecStart=/home/ubuntu/tickmind/.venv/bin/python watch_signals.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now tickmind-watch
journalctl -u tickmind-watch -f
```

The first run back-fills history into `state/events.jsonl` without notifying.
The browser session is saved in `state/session.json`, so the watcher only signs in
again when that session expires. `lag_s` in each event is the time between the site's
`postedAt` and when the watcher saw it.

### Amazon Linux 2023 (the jito FRA VM)

`playwright install --with-deps` only knows apt, so on AL2023:

```bash
python3.12 -m venv .venv && .venv/bin/pip install pandas numpy playwright
sudo dnf install -y libatomic nss atk at-spi2-atk cups-libs libdrm libxkbcommon libXcomposite \
  libXdamage libXrandr libXfixes mesa-libgbm pango alsa-lib
.venv/bin/playwright install chromium
```

Deployed 2026-11-13 on `ec2-user@ec2-3-65-33-160` in `~/tickmind` (systemd `tickmind-watch`,
`tickmind-ui`; data in `~/data/XAUUSD`). Without `TICKMIND_EMAIL`/`TICKMIND_PASSWORD` the watcher
runs **API-only**: new signals, closes and results are caught, but active entry/stop/target stay 0.

## Monitoring UI (for cloudflared)

`ui_server.py` is stdlib-only and serves one page on `127.0.0.1:8077`. The page shows:

- the watcher heartbeat and the active signal's levels
- live watcher events
- site claims vs the M1 replay: headline numbers, where the gap comes from, cumulative-pips
  chart, agreement matrix, fill models, monthly table, and a per-signal table with a
  "site win, replay loss" filter

Every `REFRESH_HOURS` (default 6) it re-fetches `/api/signals` and re-runs the backtest.
The **Refresh backtest** button does the same on demand. Keep the HistData folder on the
server up to date (`DATA_DIR`); signals after the last bar show as "not replayed".

```bash
pip install pandas numpy        # backtest deps, same venv as the watcher
DATA_DIR=/home/ubuntu/XAUUSD UI_USER=me UI_PASSWORD='long-random' python ui_server.py
cloudflared tunnel --url http://127.0.0.1:8077          # quick test URL
```

A cloudflared hostname is public, so either set `UI_USER`/`UI_PASSWORD` (HTTP basic auth)
or put the hostname behind Cloudflare Access.

systemd (`/etc/systemd/system/tickmind-ui.service`):

```ini
[Unit]
Description=Tickmind monitor UI
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/tickmind
EnvironmentFile=/home/ubuntu/tickmind/tickmind.env
Environment=DATA_DIR=/home/ubuntu/XAUUSD
ExecStart=/home/ubuntu/tickmind/.venv/bin/python ui_server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Add `UI_USER=` / `UI_PASSWORD=` to `tickmind.env`. The watcher and the UI share `state/`.

## Backtest

```bash
python fetch_signals.py signals_raw.csv          # public API snapshot
python backtest_signals.py signals_raw.csv ~/Documents/Markets/Data/XAUUSD
```

Writes `results/summary.json` (read by the UI) and one CSV of trades per model. The primary model
is "touch": an order at the stated entry, filled once price trades through it within 4 h
of the post. The site's net-pips figure is reproduced by booking every loss at −50 pips.

`signals_raw.csv` is a snapshot of `/api/signals`. As of 2026-11-13 it has two target
typos (fixed by dividing by 100), and one 0.5-dollar stop (tg-7772) that is excluded.
