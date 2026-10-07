# Deploying leadlag on AWS Tokyo

This is shadow-only recording. The host makes no signed calls that trade and has no order path.

## What runs where

| Unit | Process | Address | CPUs | Priority |
|---|---|---|---|---|
| `leadlag` | trader: Binance feeds → parse → engine; frames to the observer through a non-blocking ring | admin `127.0.0.1:8060` (halt, status) | **2–3, alone** | Nice −5 |
| `leadlag-observer` | tape writer (`/data/leadlag/tape`) and feed statistics | unix socket `/run/leadlag/observer.sock`, `127.0.0.1:8061` | 0–1 | Nice 5 |
| `leadlag-status` | read-only page, answered from a 5 s cache | `127.0.0.1:8062` | 0–1 | Nice 10, CPUWeight 20, 256 MB |
| `cloudflared` | outbound tunnel to `ll.<your-domain>` behind Cloudflare Access | n/a | 0–1 | Nice 10, CPUWeight 20 |
| `leadlag-research@<job>` | archive fetches and research jobs | n/a | 0–1 | Nice 19, idle I/O |

- **The trader never serves a viewer.** The status process reads `/v1/status`, a pre-serialized buffer the
  trader rebuilds every 2 s, once every 5 s.
- **The trader never touches the disk.** If the observer stops, the trader keeps running and counts the
  bytes it discards.
- **Only 8062 is published**, and only through the tunnel. No inbound port is open apart from SSH from
  your IP.

## 1. Pick the availability zone (about 2 h, a few dollars)

Binance runs in AWS ap-northeast-1, but which AZ it sits in isn't public, so measure it.

1. Launch a **t3.micro** (Ubuntu 24.04, x86_64) in each of the zones **apne1-az1**, **apne1-az2** and
   **apne1-az4**.
   - These are zone IDs. The names (`ap-northeast-1a`/`c`/`d`) map to different IDs in each account,
     so check **EC2 → Settings → Zones**, or the subnet list's "Availability Zone ID" column.
   - Give the security group SSH access from your IP only.
2. On the Mac, build the probe and copy it to each instance:

   ```sh
   cd services/leadlag-go
   GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -o /tmp/llprobe ./cmd/llprobe
   for h in <ip-az1> <ip-az2> <ip-az4>; do scp /tmp/llprobe ubuntu@$h:; done
   ```

3. Start all three probes at the same time, so they see the same market:

   ```sh
   ssh ubuntu@<ip> 'sudo timedatectl set-ntp true; nohup ./llprobe --duration 30m > probe.log 2>&1 &'
   # 31 minutes later:
   ssh ubuntu@<ip> 'cat probe.log; cat probe-apne1-az*.json' > probe-<az>.txt
   ```

4. Pick the AZ with the lowest **REST warm RTT p50/p99** and **bookTicker recv−E p50/p99**.
   - Feed lag includes each host's clock offset, which is printed. If offsets differ by more than a
     millisecond, compare the RTTs rather than the feed lags.
   - Write the three results in the table at the end of this file, then terminate the probes.

## 2. Launch the host

| Item | Value |
|---|---|
| Type | **c7i.xlarge** (4 vCPU, 8 GiB) in the chosen AZ |
| Image | Ubuntu 24.04 LTS x86_64 |
| Root volume | 30 GB gp3 |
| Data volume | **500 GB gp3** (3000 IOPS, 125 MB/s), a separate volume |
| Network | Elastic IP; security group: inbound SSH from your IP only |
| IMDS | v2 required (llprobe reads the AZ ID through it) |

The tape grows by about 0.2–0.3 GB/day per recorded symbol. 14 symbols take 3–4 GB/day, so 500 GB
lasts about 4 months. EBS volumes can be grown online (`growpart` + `resize2fs`).

## 3. Host setup (once, as `ubuntu`)

```sh
# user
sudo adduser --disabled-password --gecos "" ll
sudo mkdir -p /home/ll/.ssh && sudo cp ~/.ssh/authorized_keys /home/ll/.ssh/ && sudo chown -R ll:ll /home/ll/.ssh
# ll may restart only its own units (used by deploy/push.sh)
echo 'll ALL=(root) NOPASSWD: /usr/bin/systemctl restart leadlag-observer leadlag leadlag-status, /usr/bin/systemctl start leadlag-research@*' \
  | sudo tee /etc/sudoers.d/leadlag && sudo chmod 440 /etc/sudoers.d/leadlag

# data volume: check the device name, because mkfs erases it
lsblk
sudo mkfs.ext4 -L lldata /dev/nvme1n1
sudo mkdir -p /data && echo 'LABEL=lldata /data ext4 defaults,noatime,nofail 0 2' | sudo tee -a /etc/fstab
sudo mount -a && sudo mkdir -p /data/leadlag && sudo chown ll:ll /data/leadlag

# clock: Amazon Time Sync, required for the feed-lag numbers to mean anything
sudo apt-get update && sudo apt-get install -y chrony
echo 'server 169.254.169.123 prefer iburst minpoll 4 maxpoll 4' | sudo tee /etc/chrony/sources.d/aws.sources
sudo systemctl restart chrony && sleep 30 && chronyc tracking   # "System time" offset should be under 0.1 ms
ls /dev/ptp* 2>/dev/null && echo "PTP clock present: add 'refclock PHC /dev/ptp0 poll 0 delay 0.000010 prefer' to chrony"

# keep everything except the trader off CPUs 2-3
sudo sed -i 's/^#\?CPUAffinity=.*/CPUAffinity=0 1/' /etc/systemd/system.conf
sudo systemctl daemon-reexec

# directories
sudo -u ll mkdir -p /home/ll/leadlag/releases /home/ll/leadlag/jobs
```

**Config.** Create `/home/ll/leadlag/.env` from `deploy/env/.env.example`, owned by `ll` with mode 600.
`LL_FOLLOWERS` sets which alts are recorded. Until M3 freezes the universe, the default is the top 12
screen candidates.

## 4. Install and start

On the Mac:

```sh
cd services/leadlag-go
scp -r deploy ll@<eip>:/home/ll/leadlag/deploy-src
LL_HOST=ll@<eip> deploy/push.sh --no-restart      # build, upload, point `current` at it
```

On the host:

```sh
sudo /home/ll/leadlag/deploy-src/install.sh
sudo systemctl enable --now leadlag-observer leadlag leadlag-status
curl -s localhost:8060/health; curl -s localhost:8061/health; curl -s localhost:8062/health
journalctl -u leadlag -n 20 --no-pager           # "observer connected", two "feed connected"
cp ~/probe-*.json /home/ll/leadlag/probe.json 2>/dev/null || true   # optional: probe panel on the page
```

The start order is observer → trader → status. The trader waits up to 30 s (`LL_WAIT_OBSERVER`) for the
observer, so the tape begins with the run's hello.

## 5. Publish the status page (cloudflared + Access)

This follows `infra/cloudflare/README.md`, with its own tunnel:

```sh
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared

cloudflared tunnel login                              # pick the domain in the browser
cloudflared tunnel create leadlag-status
cloudflared tunnel route dns leadlag-status ll.<your-domain>
sudo mkdir -p /etc/cloudflared && sudo cp ~/.cloudflared/<TUNNEL-ID>.json /etc/cloudflared/
sudo cp /home/ll/leadlag/deploy-src/cloudflared/config.example.yml /etc/cloudflared/config.yml
sudo nano /etc/cloudflared/config.yml                 # <TUNNEL-ID> twice, <your-domain>
sudo cloudflared --config /etc/cloudflared/config.yml service install
sudo systemctl daemon-reload && sudo systemctl restart cloudflared   # picks up priority.conf from install.sh
```

**Create the Access application before sharing the link.** Until it exists, anyone who knows the hostname
can open the page. In **Zero Trust → Access → Applications → Add → Self-hosted**:
- **Domain:** `ll.<your-domain>`
- **Session:** 24 h
- **Policy:** Allow, with your email list
- **Login method:** One-time PIN

To verify:

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://ll.<your-domain>/api/summary   # 302 or 403, never 200
```

Then open the page in a private browser window. It must ask for the email code first.

## 6. Finish M1 on the host: the screen-window archive

```sh
sudo systemctl start leadlag-research@fetch-screen      # runs /home/ll/leadlag/jobs/fetch-screen.sh
journalctl -u leadlag-research@fetch-screen -f
```

This downloads 2025-10-01 → 2026-01-31 for BTC, ETH and the 30 candidates (tens of GB). It runs at
idle priority on CPUs 0–1 and resumes if interrupted. The candidate list is saved to
`/data/leadlag/archive/screen-candidates.txt` and the integrity table to `verify-screen.txt`.

## 7. Acceptance checks (after 24 h)

- The page shows **recording**, both feeds are up, and `tape holes` is 0.
- **Ring drops are 0**, and the ring's maximum use is below 1%.
- **Engine p99 (60 s) is under 20 µs**, and **recv → done p99 is under 100 µs** (plan targets).
- The tape manifests (`/data/leadlag/tape/<day>/*.json`) cover every hour, with `gaps: 0`.
- **Replay parity holds:**
  1. Run `/home/ll/leadlag/current/leadlag --replay /data/leadlag/tape/<day>`. Each run segment prints
     `complete true`.
  2. After a restart, the stopped run's digest in `journalctl -u leadlag | grep final` equals its replay
     digest.
- `agg E = next T` and `agg E−T` on the page are the measured aggTrade publication delay. Copy the
  24-hour values into `research/PREREG.md` before M3, because they set when an archive trade counts as
  observable.

## Day to day

| Task | How |
|---|---|
| Logs | `journalctl -u leadlag -f` (and `-u leadlag-observer`, `-u leadlag-status`) |
| Upgrade | `LL_HOST=ll@<eip> deploy/push.sh` from the Mac. It restarts observer → trader → status. |
| Roll back | `ln -sfn /home/ll/leadlag/releases/<rev> /home/ll/leadlag/current && sudo systemctl restart leadlag-observer leadlag leadlag-status` |
| Halt (no effect in record mode; stops new shadow entries in M5) | `curl -XPOST localhost:8060/v1/halt` over SSH; `…/v1/resume` |
| Disk | The page shows usage. Taping pauses at `LL_DISK_MAX_PCT` (90%) and resumes 5 points lower; the trader keeps running. |
| Tape on another machine | `rsync -av ll@<eip>:/data/leadlag/tape/<day> .` then `leadlag --replay <day>` |
| Status page down | `systemctl status leadlag-status cloudflared`. The trader doesn't depend on either. |

## AZ probe results

| AZ ID | REST p50 / p99 ms | bookTicker recv−E p50 / p99 ms | clock offset ms | chosen |
|---|---|---|---|---|
| apne1-az1 | | | | |
| apne1-az2 | | | | |
| apne1-az4 | | | | |
