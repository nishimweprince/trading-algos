# Publishing the status page (Cloudflare Tunnel + Access)

The read-only status page (`ofi-status`, `127.0.0.1:8040` on the VM) is shared
with colleagues through a **Cloudflare Tunnel**: `cloudflared` on the VM
connects *out* to Cloudflare, so no inbound port is opened on the VM.
**Cloudflare Access** sits in front: only the email addresses you list can
open it, after a one-time code sent to that address. Both are on Cloudflare's
free plan (Zero Trust is free up to 50 users).

Only the status page goes through the tunnel. The scalper's API (with the kill
switch), the order gateway and notification-service stay private.

## Prerequisites

- A domain on Cloudflare (its nameservers pointed at Cloudflare). A subdomain
  such as `status.<your-domain>` will be created for the page.
- `ofi-status` running on the VM:

  ```sh
  cd ~/trading-algos/infra/systemd
  sudo cp ofi-status.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now ofi-status
  curl -s localhost:8040/health/live          # {"status":"ok"}
  ```

- The daily summaries the page reads. New days are written by the daily-check
  timer; backfill the days recorded so far once:

  ```sh
  cd ~/trading-algos/services/ofi-scalper-service
  for d in $(ls /data/ofi/raw/dev/BTCUSDT | sort); do
    day="${d:0:4}-${d:4:2}-${d:6:2}"
    [ "$day" = "$(date -u +%F)" ] && continue          # today is still being recorded
    ../../.venv/bin/ofi-daily-check --profile dev --date "$day" --no-notify > /dev/null
  done
  ```

## 1. Install cloudflared (on the VM)

```sh
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared
```

## 2. Create the tunnel

```sh
cloudflared tunnel login                     # opens a URL: pick your domain in the browser
cloudflared tunnel create ofi-status         # prints the tunnel ID, writes ~/.cloudflared/<ID>.json
cloudflared tunnel route dns ofi-status status.<your-domain>
```

## 3. Run it as a service

```sh
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/<TUNNEL-ID>.json /etc/cloudflared/
sudo cp ~/trading-algos/infra/cloudflare/config.example.yml /etc/cloudflared/config.yml
sudo nano /etc/cloudflared/config.yml        # set <TUNNEL-ID> (twice) and <your-domain>
sudo cloudflared --config /etc/cloudflared/config.yml service install
sudo systemctl enable --now cloudflared
systemctl status cloudflared --no-pager | head -5
```

## 4. Put Cloudflare Access in front (before sharing the link)

In the Cloudflare dashboard: **Zero Trust → Access → Applications → Add an
application → Self-hosted**.

- **Application domain:** `status.<your-domain>`.
- **Session duration:** 24 hours.
- **Policy:** name it "colleagues", action **Allow**, include **Emails**: list
  each colleague's address (or **Emails ending in** `@your-company.com`).
- **Login methods:** One-time PIN (a code is emailed; no accounts needed).

Until this application exists, the tunnel serves the page to anyone who knows
the hostname, so create it before handing out the link.

## 5. Verify

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://status.<your-domain>/api/summary   # 302 or 403, not 200
```

- In a private browser window, `https://status.<your-domain>` must show the
  Cloudflare login, and the page only after entering an allowed email's code.
- An email not on the list must be refused.

## Day to day

- **Add or remove a colleague:** edit the policy's email list; removal takes
  effect at their next session check.
- **Roadmap on the page:** `services/ofi-scalper-service/status_roadmap.json`;
  update it in the same commit as the work, then `git pull` on the VM.
- **If the page is down:** `systemctl status ofi-status cloudflared`. A crash of
  `ofi-status` sends a Telegram alert like the other services.
