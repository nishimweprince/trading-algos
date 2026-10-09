# Deploying leadlag on AWS Tokyo

This is shadow-only recording. The host makes no signed calls that trade and has no order path.

Sections 1–7 describe the **dedicated** AWS host the measurements are meant for. To run next to other
services on an existing machine (as on the ofi-scalper VM), follow [Shared host](#shared-host) instead;
it reuses §3–4 with the differences listed there.

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
| Image | Ubuntu 24.04 or 26.04 LTS x86_64 |
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
# Ubuntu 26.04 ships sudo-rs, which rejects wildcards and can lock sudo on a bad
# file: validate a temp copy first, and list each research job explicitly.
T=$(mktemp)
echo 'll ALL=(root) NOPASSWD: /usr/bin/systemctl restart leadlag-observer leadlag leadlag-status, /usr/bin/systemctl start leadlag-research@fetch-screen.service' > $T
sudo visudo -cf $T && sudo install -m 440 -o root -g root $T /etc/sudoers.d/leadlag; rm -f $T

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
sudo systemctl daemon-reexec   # applies to units started from now on; reboot once so every daemon moves to 0-1

# directories
sudo -u ll mkdir -p /home/ll/leadlag/releases /home/ll/leadlag/jobs
```

**Without `push.sh`.** Build on the host from a checkout (Go from go.dev, sha256-checked):

```sh
cd ~/trading-algos/services/leadlag-go && REV=$(git rev-parse --short HEAD) && OUT=$(mktemp -d)
CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=$REV" -o $OUT/ ./cmd/...
sudo install -d -o ll -g ll /home/ll/leadlag/releases/$REV && sudo install -o ll -g ll -m 755 $OUT/* /home/ll/leadlag/releases/$REV/
sudo -u ll ln -sfn /home/ll/leadlag/releases/$REV /home/ll/leadlag/current
sudo install -o ll -g ll -m 755 deploy/jobs/*.sh /home/ll/leadlag/jobs/
sudo systemctl restart leadlag-observer leadlag leadlag-status
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
sudo systemctl start leadlag-research@fetch-screen.service   # runs /home/ll/leadlag/jobs/fetch-screen.sh
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

## Shared host

leadlag also runs next to other services on one machine. It runs this way on the ofi-scalper VM (Azure,
2026-10-08), which records order books and paper-trades on Binance demo. The recorder, observer and status
page are the same; what changes is that the host isn't leadlag's alone.

| | Dedicated (§1–7) | Shared host (ofi VM) |
|---|---|---|
| Machine | c7i.xlarge in the measured AWS AZ | Azure Standard_D4ads_v6, 4 vCPU / 16 GB, **japanwest (Osaka)** |
| CPUs | trader alone on 2–3; `system.conf` moves everything else to 0–1 | nothing pinned; trader at normal priority |
| Data | separate 500 GB volume at `/data` | `/data` is a directory on the 247 GB root disk, shared with the ofi recordings |
| Clock | chrony on Amazon Time Sync | chrony on Hyper-V's PTP clock (`PHC0`), already ~0.2 µs; leave it |
| cloudflared | own tunnel, local `config.yml` | the existing tunnel is remotely managed (token): add a public hostname in the dashboard |

**What to skip from §3:** the AZ probe (§1), `mkfs` and the fstab entry, the chrony source (Amazon Time
Sync exists only on EC2), and the `CPUAffinity=0 1` change to `/etc/systemd/system.conf`. That change
would squeeze every other service on the host onto two CPUs. Without `push.sh`, the `ll` sudoers line
isn't needed either.

**Install and start** (build on the host, Go from go.dev with its sha256 checked, as in §3):

```sh
sudo adduser --disabled-password --gecos "" ll
sudo install -d -o ll -g ll -m 750 /home/ll/leadlag /home/ll/leadlag/releases /home/ll/leadlag/jobs
sudo install -d -o ll -g ll /data/leadlag /data/leadlag/tape /data/leadlag/archive
# build, install the release and point `current` at it: "Without push.sh" in §3
# config: /home/ll/leadlag/.env from deploy/env/.env.example (ll, mode 600)
sudo deploy/install.sh --shared-host
sudo systemctl enable --now leadlag-observer leadlag leadlag-status
```

`--shared-host` adds drop-ins (`deploy/systemd/shared-host/`):
- They clear `CPUAffinity`, and run the trader at Nice 0, so it never outranks a live trading process next
  to it.
- They cap `leadlag-research@` jobs at 4 GB (`MemoryMax`, `OOMScoreAdjust=900`), so the OOM killer picks a
  research job before a recorder.
- They don't install the cloudflared priority drop-in, so an existing tunnel keeps its settings.

**Probe:** run it once and keep the result for the page:

```sh
sudo systemd-run --unit=leadlag-probe --uid=ll --gid=ll --nice=10 \
  /home/ll/leadlag/current/llprobe --duration 30m --out /home/ll/leadlag/probe.json
```

It costs about 64 request weight per minute against the IP's 2400. The ofi-scalper budgets 1200 of those,
so the two fit together. Off EC2, llprobe reads Azure's IMDS and labels the host `azure:<region>`.

**Publish the page (remotely managed tunnel):** in Zero Trust → Networks → Tunnels → the host's tunnel →
Public Hostname → Add, set `ll.<your-domain>` → `http://127.0.0.1:8062`. Then create the Access
application as in §5, before sharing the link. Nothing changes on the host.

**Disk budget:**
- The tape takes 0.2–0.3 GB/day per symbol, so the default 14 symbols take 3–4 GB/day.
- The ofi recorder adds about 4 GB/day (6 symbols, full depth).
- That is roughly 8 GB/day together, against about 230 GB free: around **4 weeks**, and less once
  `fetch-screen` adds its tens of GB.
- Taping pauses at 90% full (`LL_DISK_MAX_PCT`), before the ofi recorder's 10 GB floor, so the ofi
  recordings are the last to stop.
- Attach a data disk at `/data`, or shorten `LL_FOLLOWERS`, before running the archive jobs or a long
  recording.

**Memory:** the trader's heap is about 75 MB (`GOMEMLIMIT=1GiB`), and the status page is capped at 256 MB.
The ofi research pipeline can use up to 10 GB on its own. Run leadlag research jobs only when it isn't
running.

**What its numbers mean:**
- Binance's matching engine is in AWS ap-northeast-1 (Tokyo), and this VM is in Osaka.
- Feed lag and REST round trips here describe Osaka → Tokyo, not the AWS host that `research/PREREG.md`
  freezes L* and δ from:
  - **δ** (the aggTrade publication delay, `E − T`) is measured at the exchange and carries over.
  - **L*** (from the REST RTT) does not.
- The trader shares the CPUs with Python services, so engine and receive → done percentiles are noisier
  than on the dedicated host.
- Treat a shared host as a **recorder and status page**. Keep the M4 L* and the M5 shadow (G5) on the
  measured AWS host, or amend PREREG for this host before computing any statistic from it.

**Shadow mode (exploratory, `research/PREREG.md` Part 2):**

```sh
sudo install -o ll -g ll -m 600 deploy/env/shadow.example.json /home/ll/leadlag/shadow.json   # set latency_ms from the probe
# in /home/ll/leadlag/.env: LL_MODE=shadow and LL_SHADOW_PARAMS=/home/ll/leadlag/shadow.json
sudo systemctl restart leadlag-observer leadlag leadlag-status
journalctl -u leadlag -n 5 --no-pager | grep "shadow engine"          # run, params sha256, latency
```

- Signals start after the detector's 30-minute warm-up.
- Every completed trade goes through the ring to the observer, which appends it to
  `/data/leadlag/shadow/<UTC day>.jsonl` (`LL_JOURNAL_DIR`). The trader still never writes to disk.
- The page's Shadow panel shows totals, per-follower results and the last trades, labelled exploratory.
- `curl -XPOST localhost:8060/v1/halt` stops new entries; open positions still close at their horizon.
- `sudo systemctl start leadlag-research@shadow-verify.service` replays the tape with the same params,
  only for the runs whose hello carries their sha256. It fails if the replayed trades differ from the
  journal.
- A halt isn't on the tape, so check a halted run's journal by hand.

**Start a new epoch** (a new run label, e.g. `explore-2`, PREREG Part 3). The trader keeps its stats in
memory, so a restart resets them. The steps below make the journal start empty too. The tape is kept
as research data.

```sh
# on the host, from an up-to-date checkout: build and install (see "Without push.sh"). If the host has
# no GitHub credentials, pull with a one-shot token header that is never stored:
#   git -c http.extraHeader="Authorization: Basic $(printf 'x-access-token:%s' "$TOKEN" | base64 -w0)" pull --ff-only origin main
sudo systemctl stop leadlag-status leadlag leadlag-observer
old=$(sudo python3 -c 'import json; print(json.load(open("/home/ll/leadlag/shadow.json"))["run"])')
sudo -u ll mkdir -p /data/leadlag/shadow/$old && sudo -u ll sh -c "mv /data/leadlag/shadow/*.jsonl /data/leadlag/shadow/$old/"
sudo -u ll cp /home/ll/leadlag/shadow.json /home/ll/leadlag/shadow-$old.json
sudo install -o ll -g ll -m 600 deploy/env/shadow-explore-2.example.json /home/ll/leadlag/shadow.json
sudo systemctl start leadlag-observer leadlag leadlag-status
journalctl -u leadlag -n 5 --no-pager | grep "shadow engine"          # run, trigger, exit, params sha256
```

`shadow-verify` reads a run's archived journals from `<journal dir>/<run>/`. To re-check an old run
(the unit's `.env` always names the current params), run the job directly:
`sudo -u ll LL_SHADOW_PARAMS=/home/ll/leadlag/shadow-explore-1.json nice -n 19 bash /home/ll/leadlag/jobs/shadow-verify.sh`.

**First numbers on the ofi VM (2026-10-08, 14 symbols, ofi research running):**

| Measure | ofi VM (azure:japanwest) | AWS Tokyo (README, 2026-10-07) |
|---|---|---|
| REST warm RTT p50 / p99 | 10.75 / 442 ms | 3.7 ms p50 |
| bookTicker recv − E p50 / p99 | 4.9 / 9.7 ms | 1.2–2.2 / 2–5 ms |
| aggTrade recv − E p50 / p99 (BTC) | 5.4 / 6.9 ms | — |
| aggTrade publish delay E − T p90 (BTC / ETH) | 151 / 152 ms | ≈ 150 ms |
| Clock offset (Binance − local) | +0.75 ms | −0.09 ms |
| Engine p99 (60 s) | 27.6 µs idle, 55 µs during ofi research | 21.5 µs |
| Ring drops / tape holes / id gaps (first 31 min, 4.8 M events) | 0 / 0 / 0 | 0 / 0 / 0 |
| Tape growth | ≈ 4.3 GB/day (14 symbols) | 3–4 GB/day estimate |

- `E − T` matches AWS, because Binance sets it: this host's δ is valid.
- The round trip is about 3× AWS's: L here is 20 ms against L* = 5 ms on AWS.

## Day to day

| Task | How |
|---|---|
| Logs | `journalctl -u leadlag -f` (and `-u leadlag-observer`, `-u leadlag-status`) |
| Upgrade | `LL_HOST=ll@<eip> deploy/push.sh` from the Mac. It restarts observer → trader → status. |
| Roll back | `ln -sfn /home/ll/leadlag/releases/<rev> /home/ll/leadlag/current && sudo systemctl restart leadlag-observer leadlag leadlag-status` |
| Halt (no effect in record mode; stops new shadow entries) | `curl -XPOST localhost:8060/v1/halt` over SSH; `…/v1/resume` |
| Shadow trades | `/data/leadlag/shadow/<day>.jsonl`; check against the tape with `systemctl start leadlag-research@shadow-verify.service` |
| Disk | The page shows usage. Taping pauses at `LL_DISK_MAX_PCT` (90%) and resumes 5 points lower; the trader keeps running. |
| Tape on another machine | `rsync -av ll@<eip>:/data/leadlag/tape/<day> .` then `leadlag --replay <day>` |
| Status page down | `systemctl status leadlag-status cloudflared`. The trader doesn't depend on either. |

## AZ probe results

| AZ ID | REST p50 / p99 ms | bookTicker recv−E p50 / p99 ms | clock offset ms | chosen |
|---|---|---|---|---|
| apne1-az1 | | | | |
| apne1-az2 | | | | |
| apne1-az4 | | | | |
