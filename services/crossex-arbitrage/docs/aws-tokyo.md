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
