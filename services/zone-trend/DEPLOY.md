# Deploying zone-trend

Target: one small VPS in Tokyo or Singapore, running systemd. Do not go live until the M4 acceptance table passes (`zt accept`) and two weeks of shadow parity are at least 95% (`zt parity`).

## Host
1. Create a user `zt`. Clone the repo to `/home/zt/trading-algos`. Run `uv sync --package zone-trend --python 3.12`.
2. Set up time sync (`chrony` or `systemd-timesyncd`). Live mode refuses to start with more than 500 ms of drift from Binance.
3. Copy `infra/systemd/zone-trend-*` to `/etc/systemd/system/`, then run `systemctl daemon-reload`.

## Secrets: `services/zone-trend/.env.zt` (chmod 600, never committed)
```
ZT_SYMBOLS=BTCUSDT,ETHUSDT,...           # or omit and build data/universe/history.json
ZT_CAPITAL=500
ZT_STAGE=micro                           # micro | half | full | auto (advances by the gates)
ZT_WF_DIR=runs/wf-2021-01-2026-09        # the walk-forward the weekly report compares with
NOTIFICATIONS_ENABLED=true
NOTIFICATION_SERVICE_URL=http://127.0.0.1:3010
NOTIFICATION_API_KEY=replace-with-the-notification-service-key
NOTIFICATION_CHANNELS=TELEGRAM
ZT_WARNING_CHANNELS=TELEGRAM,EMAIL
ZT_CRITICAL_CHANNELS=TELEGRAM,EMAIL,SMS,WHATSAPP
# live only:
BINANCE_FUTURES_ENV=mainnet
BINANCE_FUTURES_TRADING_API_KEY=replace-with-the-futures-only-key
BINANCE_FUTURES_TRADING_API_SECRET=replace-with-its-secret
ZT_LIVE_ACK=real-money-zone-trend
ZT_BACKUP_REMOTE=                        # an rclone remote for off-site DB copies
```

Binance key settings:
- futures trading only;
- withdrawals disabled;
- IP-whitelisted to this host.

Keep a separate read-only key for monitoring. Use a dedicated sub-account if you can. Equity only counts zone-trend's own positions, so a shared account works too, but the reconciler will warn about positions it doesn't manage.

## Units
| Unit | What it does |
|---|---|
| `zone-trend-shadow` | strategy on live data, paper fills, no orders |
| `zone-trend-live` | real money; not enabled; started by hand; conflicts with shadow |
| `zone-trend-status` | dashboard, `/health` and `/metrics` on 127.0.0.1:8060 |
| `zone-trend-data-sync.timer` | daily Vision sync and validation (for parity and reports) |
| `zone-trend-backup.timer` | daily `sqlite3 .backup`, 30 days locally, optional rclone off-site |
| `zone-trend-unit-alert@` | critical alert when any unit fails |

Run `systemctl enable --now zone-trend-status zone-trend-shadow zone-trend-data-sync.timer zone-trend-backup.timer`.

Point an external uptime monitor at `/health` (through the tunnel). A dead bot can't alert on itself, so this is the only thing that will notice. Publish the status page only through a tunnel with access control (`infra/cloudflare`), never on a public port.

## Going live
1. Check that `zt accept --wf $ZT_WF_DIR` passes and that `zt parity --from <2 weeks ago> --to <today>` shows at least 95%.
2. Run `systemctl stop zone-trend-shadow && systemctl start zone-trend-live`. Startup checks one-way mode, sets ISOLATED margin and leverage per symbol, replays history from listing, and reconciles before trading.
3. Stage gates are evaluated daily. With `ZT_STAGE=auto` the bot advances micro → half → full on its own; otherwise it alerts when a gate passes.

## Operations
- **Kill switch:** `zt kill`. It closes every zone-trend position at market, cancels `zt-` orders on flat symbols, and halts.
- **After a −25% halt or a kill:** review, then run `zt live --resume` once by hand.
- **Upgrades:** stop the unit (positions keep their exchange stops), pull, `uv sync`, start. The startup reconcile repairs anything that changed in between.
