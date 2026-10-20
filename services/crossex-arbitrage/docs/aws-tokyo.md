# AWS Tokyo deployment

Target region: `ap-northeast-1`, pending the M0 probe comparison with
`ap-southeast-1` (see `RUNBOOK.md`). Binance USDⓈ-M is matched in Tokyo and
Bybit in Singapore, so the host is chosen by the worse venue's p99, not by
either one's average.

## Host

A compute-optimised instance (c7i.large or c7gn.large) is enough: the
decision path runs at about 4 µs p99, and feeds and recording are the main
load. Attach a gp3 volume of at least 200 GB at `/data`.

Amazon Linux 2023:

```sh
sudo dnf install -y gcc-c++ cmake ninja-build openssl-devel zstd chrony git
sudo systemctl enable --now chronyd && chronyc tracking
curl-config --protocols | tr ' ' '\n' | grep -qi '^wss$' && echo "curl has wss" || echo "build curl"
```

If the system libcurl lacks WebSockets (curl < 8.11 usually does), build
one into `/opt/curl` and point CMake at it:

```sh
sudo dnf install -y libpsl-devel
curl -LO https://curl.se/download/curl-8.11.1.tar.xz && tar xf curl-8.11.1.tar.xz && cd curl-8.11.1
./configure --prefix=/opt/curl --with-openssl --enable-websockets && make -j && sudo make install
cmake --preset release -DCMAKE_PREFIX_PATH=/opt/curl -DCMAKE_BUILD_RPATH=/opt/curl/lib
```

## Install

```sh
sudo useradd --system --home /opt/trading-algos --shell /sbin/nologin crossex
sudo mkdir -p /data/crossex && sudo chown crossex:crossex /data/crossex
sudo git clone <repo> /opt/trading-algos && cd /opt/trading-algos/services/crossex-arbitrage
git checkout feat/crossex-arbitrage
cmake --preset release && cmake --build --preset release -j && ctest --preset release
sudo install -m 0600 config.example.env /etc/crossex.env   # then add read-only keys
sudo cp ../../infra/systemd/crossex-*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
```

Before `systemctl enable`, run `check-keys`, the probe (M0), and a 10-minute
`shadow --duration-seconds 600`. Then fill in the PREREG block and commit it.

## Record at deployment

```
region / instance type / AMI:
CPU model:
compiler:
libcurl version (with wss):
commit:
binary sha256:
probe Tokyo     (binance signed p99 / bybit signed p99 / feed age p99):
probe Singapore (binance signed p99 / bybit signed p99 / feed age p99):
```

### 2026-10-20 (ap-northeast-1, 57.183.17.58)

```
region / instance type / AMI: ap-northeast-1 / 4 vCPU 16 GB / Amazon Linux 2023
CPU model: Intel(R) Xeon(R) CPU E5-2686 v4 @ 2.30GHz (x86_64)
compiler: g++ 11.5.0 (cmake 4.4.4 from pip; distro cmake 3.22 is too old)
libcurl version (with wss): 8.11.1 built from source into /opt/curl
  (distro libcurl 8.21.0 advertises WSS in curl-config but its runtime
  protocol list lacks wss, so the service refused to start against it)
commit: 7df9070 + uncommitted fixes (kMaxDepthChanges 2048, replay header
  order tolerance, Binance connect pre-roll, CXA_ALLOW_TRADING_KEYS valve;
  see git status)
binary sha256: 1a46b32ebb987a5d8d35...
keys: binance read-only verified (taker 500 ppm); bybit key has trade
  permission, running with CXA_ALLOW_TRADING_KEYS=1 at operator direction
  (shadow has no order-entry code; prefer a read-only key when convenient)
signed RTT p50 from Tokyo: binance 4.3 ms, bybit 38.8 ms (measured)
smoke (full shadow with keys, 15 symbols, 90 s): 94k depth events,
  gaps [0,0], gapcheck ok, replay deterministic (hash stable across runs)
service: crossex-shadow enabled+active since 2026-10-20T14:38Z, journal
  /data/crossex/crossex-%Y%m%dT%H%M%SZ.jsonl, daily restart, hourly zstd
data disk: /dev/xvdb (100 GB, xfs) mounted at /data via fstab
status UI: http://127.0.0.1:8070/ (loopback only; expose via cloudflared)
```

### 2026-10-20T22:48Z — PREREG restart (maker/taker + wider basket)

```
prior journal archived: /data/crossex/archive/crossex-20261020T143851Z.jsonl
exec_mode: maker_taker (CXA_MAKER_TIMEOUT_MS=2000)
trade_notional: 250; min_net_ppm: 0; latency: pessimistic
basket: 30 dual-listed USDT perps (see PREREG.md)
fees: binance taker/maker 500/200; bybit 550/200 (account rates)
ops fixes for 30-symbol start: throttle commissionRate + Bybit instrument
  GETs; drain Binance WS during REST snapshots; RestartSec=30
service: crossex-shadow stable since ~2026-10-20T22:48Z (30/30 books ok)
gate: ≥14 days, ≥150 cycles, legging ≤10%; mean net reported not gating
journal: /data/crossex/crossex-*.jsonl (new run after archive)
```
