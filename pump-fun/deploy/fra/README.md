# PumpDesk — Frankfurt deploy (Azure Germany West Central)

Branch `feat/fra-lean`. Target state: **dry-run** (`config.yaml` → `mode: dry-run`). Going live is a separate decision, made after the pool replay and a dry-run window.

```
                  ┌──────────────── Azure germanywestcentral, 4 vCPU ────────────────┐
LaserStream fra ─▶│ pump-desk-trader   (cores 0-1)  detect → fast screen → buy/exits │─▶ Helius Sender fra (+ RPC)
                  │ pump-desk-research (cores 2-3)  enrichment, cluster warm, shadow │─▶ Helius RPC (research key)
                  │ pump-desk-dashboard(cores 2-3)  127.0.0.1:8787 (read-only)       │
                  │ cloudflared ───────────── outbound only ─────────────────────────┼─▶ Cloudflare ─▶ pumpdesk.nishimweprince.dev
                  └──────────────────── no inbound ports open ───────────────────────┘
```

## 0. Does Cloudflare Tunnel make the bot faster?

No. The trading path is LaserStream (in) → process → Sender/RPC (out), and none of it goes through Cloudflare.

The tunnel only carries the dashboard. It replaces a public IP plus open port with an outbound-only connection from `cloudflared` to Cloudflare. Browsers reach the nearest Cloudflare PoP, which hops to Frankfurt, typically adding a few ms over a direct connection.

It's still worth using, for security and simplicity:

- the VM needs no open inbound ports, so no nginx and no TLS certificates to manage;
- the hostname gets Cloudflare Access (SSO / email OTP) in front of the dashboard's Basic Auth;
- SSH can go over Access as well, so port 22 can be closed.

It does **not** remove the need for a server: the bot still runs on the Frankfurt VM.

## 1. Before cutover — exactly one writer per wallet

1. **Stop the Kigali/other instance (operator).** Use `pm2 stop all && pm2 delete all` on that host, then rotate the password that was shared in chat.
2. **Stop the US-East bot:** `ssh … basis@172.191.94.21 'pm2 stop pump-desk-main'`.
3. **The foreign-signer guard is the backstop.** If another process ever signs with the trading key, the trader halts new entries and pages Telegram (`🚨 foreign signer`).

## 2. VM

```bash
az group create -n pumpdesk-fra -l germanywestcentral
az vm create -g pumpdesk-fra -n pumpdesk-fra-1 \
  --image Canonical:ubuntu-24_04-lts:server:latest \
  --size Standard_F4s_v2 \
  --accelerated-networking true \
  --admin-username azureuser --generate-ssh-keys \
  --custom-data pump-fun/deploy/fra/cloud-init.yaml \
  --public-ip-sku Standard
# Bootstrap only: allow SSH from your IP until §5's Access SSH works, then delete the rule.
az network nsg rule create -g pumpdesk-fra --nsg-name pumpdesk-fra-1NSG -n ssh-bootstrap \
  --priority 1000 --source-address-prefixes <YOUR_IP>/32 --destination-port-ranges 22 --access Allow
```

- **Size:** `Standard_F4s_v2` gives 4 dedicated vCPUs (compute-optimised); `Standard_D4as_v5` is the alternative.
- **Why 4 cores:** the trader gets cores 0–1 to itself, and research and dashboard share cores 2–3.

## 3. Install

```bash
sudo -iu pumpdesk
git clone --branch feat/fra-lean https://github.com/nishimweprince/trading-algos.git ~/trading-algos
bash ~/trading-algos/pump-fun/deploy/fra/setup.sh   # nvm + node 26, pm2, npm ci, dashboard build, typecheck, tests
```

## 4. `.env` (`~/trading-algos/pump-fun/.env`, mode 600)

| var | value |
|---|---|
| `WALLET_PRIVATE_KEY` | trading key (copy over SSH; never paste in chat) |
| `HELIUS_HTTP_URL` | `https://mainnet.helius-rpc.com/?api-key=<TRADE_KEY>` |
| `HELIUS_RESEARCH_HTTP_URL` | `https://mainnet.helius-rpc.com/?api-key=<RESEARCH_KEY>` — a **second** key |
| `HELIUS_GRPC_URL` | `https://laserstream-mainnet-fra.helius-rpc.com` |
| `HELIUS_GRPC_TOKEN` | trade key |
| `TG_BOT_TOKEN`, `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`, `TYPESAFE_API_KEY` | as today |
| `SECONDARY_HTTP_URL`, `TERTIARY_HTTP_URL` | leave empty; `config.yaml` no longer reads them |

## 5. Cloudflare Tunnel + Access

```bash
cloudflared tunnel login                                   # browser auth, once
cloudflared tunnel create pumpdesk-fra                     # writes ~/.cloudflared/<UUID>.json
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/<UUID>.json /etc/cloudflared/pumpdesk-fra.json
sudo cp ~/trading-algos/pump-fun/deploy/fra/cloudflared/config.yml /etc/cloudflared/config.yml
cloudflared tunnel route dns pumpdesk-fra pumpdesk.nishimweprince.dev
cloudflared tunnel route dns pumpdesk-fra ssh-pumpdesk.nishimweprince.dev
sudo cloudflared service install && sudo systemctl enable --now cloudflared
```

1. **Change DNS.** `pumpdesk.nishimweprince.dev` currently returns **522** (origin unreachable). The `route dns` commands above replace the old record with the tunnel CNAME.
2. **Protect the hostnames.** In Zero Trust → Access → Applications, add a self-hosted app for both hostnames with policy *Allow → emails: <your-email>*.
3. **Switch SSH to the tunnel.** Then delete the `ssh-bootstrap` NSG rule; SSH from then on uses `cloudflared access ssh --hostname ssh-pumpdesk.nishimweprince.dev`.

## 6. Pick endpoints from the VM

```bash
cd ~/trading-algos/pump-fun && set -a && . ./.env && set +a
node --experimental-strip-types deploy/fra/probe-latency.ts 30
```

1. Keep `laserstream-mainnet-fra` and `http://fra-sender.helius-rpc.com/fast` unless the probe shows a lower p50 elsewhere, e.g. `ams`. If so, change `HELIUS_GRPC_URL` and `heliusSender.url` to that endpoint. Regional Sender hosts are plain HTTP only; HTTPS fails the handshake.
2. Paste the probe table here.

| role | host | tcp p50 | tls p50 | http p50 | http p90 |
|---|---|---|---|---|---|
| _fill in after the first run_ | | | | | |

## 7. Move the DB and start (dry-run)

On the old host:

```bash
pm2 stop pump-desk-main
sqlite3 data/scalper.db ".backup data/backup/pre-fra.db"
```

Then on the new VM:

```bash
scp -o ProxyCommand='cloudflared access ssh --hostname %h' basis@<old>:trading-algos/pump-fun/data/backup/pre-fra.db ~/trading-algos/pump-fun/data/scalper.db
cd ~/trading-algos/pump-fun
pm2 start deploy/fra/ecosystem.fra.config.cjs && pm2 save && pm2 startup   # follow the printed sudo line
pm2 logs pump-desk-trader --lines 50
```

## 8. Verify (first hour)

- `process role {role: "trader"}` appears in the trader log, and `research worker started` in the research log.
- The trader is subscribed to three LaserStream channels: the detector, the wallet stream (live only), and pre-arm (`pre-arm subscribed to pump.fun curve completions`).
- `pre-arm stats` is logged every 5 min, and `hits` grows as graduations land. A hit means the screen did zero RPC reads.
- No `foreign signer` alerts. The startup audit may warn about txs from before the DB copy; that is expected.
- `latency_samples` contains `detect_to_send_us` (dry-run simulate path, so no send). Fast-read graduations should be under 30 000 µs; pre-armed ones should be about 1 000 µs plus the buy build time.
- The dashboard is reachable only through Access, and `curl http://<public-ip>:8787` fails.

## 9. Rollback

```bash
pm2 stop all   # on the FRA VM
```

Then on the US-East host:

```bash
git checkout feat/fast-path
pm2 start pump-desk-main
```

The US-East DB still holds everything up to the cutover backup.

## 10. Racer-only mode (`feat/migration-racer`)

The migration racer is a measurement worker. It holds no wallet and no signer, and it never sends a transaction. In racer-only mode the VM runs **only** `pump-desk-racer` and `pump-desk-dashboard`. The trader and research worker stay stopped.

- **Process:** `pump-desk-racer` (`src/racer-worker.ts`, `PUMPDESK_ROLE=racer`), from `ecosystem.fra.config.cjs`.
- **Config:** `racer.enabled: true` in `config.yaml`. Plan: `.agents/plans/2026-10-05-racer-measurement-fidelity.md`.
- **Env (by name):** `HELIUS_GRPC_URL` + `HELIUS_GRPC_TOKEN` for its single LaserStream subscription, and `HELIUS_RESEARCH_HTTP_URL` for RPC (`rpc.enrichmentHttp`). No `WALLET_PRIVATE_KEY` is read.
- **Tables:** `racer_curves`, `racer_migrate_txs`, `racer_migrations`, `racer_paper_tracks`. The legacy `racer_events` / `racer_paper` tables are no longer written.

Deploy:

```bash
cd ~/trading-algos && git fetch origin && git checkout feat/migration-racer && git pull --ff-only
cd pump-fun && npm ci && npm run dashboard:build
pm2 stop pump-desk-trader pump-desk-research
cp data/scalper.db data/backup/pre-racer-$(date +%Y%m%d-%H%M).db
pm2 restart pump-desk-dashboard
pm2 start deploy/fra/ecosystem.fra.config.cjs --only pump-desk-racer || pm2 restart pump-desk-racer
pm2 save
```

Health checks:

- `racer stream subscribed` appears once in `pm2 logs pump-desk-racer`, then `racer stats` every 60 s with `connected: true` and `sinceFrameMs` under 5 000.
- `stream.migrates` grows by about 15 per hour. `stream.curves` grows whenever a curve is within ~5 SOL of completing.
- `curl -s 127.0.0.1:8787/api/racer/summary` returns `migrations.total` > 0 within about 10 minutes, with `lastMigrateAt` recent.
- `resolver.unresolved` stays small next to `resolver.resolved`.

Soak exit (plan step 5): at least 300 rows in `racer_migrations`. Then write `reports/racer-soak-<date>.md` from `/api/racer/summary`.

Rollback to the trader: `pm2 stop pump-desk-racer && git checkout feat/fra-lean && pm2 restart pump-desk-trader pump-desk-research pump-desk-dashboard`.

## Going live later (not part of this deploy)

1. Run `npm run research:pool-replay -- --days 7` and check the expectancy after fees.
2. If it's positive, start a **small** live canary: `mode: live` plus restart.
3. Check slot attribution in the `migration_to_land_slots` latency samples and `execution_json.timing.slotsFromMigration`. Sender's effect on same-slot landing can only be measured live.
