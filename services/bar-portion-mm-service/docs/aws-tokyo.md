# AWS Tokyo deployment

Target region: `ap-northeast-1`. Benchmark at least an Intel c7i and a
network-focused c7gn instance before choosing; architecture and CPU results are
not interchangeable. A dedicated instance is preferred for stable tail latency.

## Amazon Linux 2023 packages

```sh
sudo dnf install -y gcc-c++ cmake ninja-build libcurl-devel openssl-devel chrony
sudo systemctl enable --now chronyd
chronyc tracking
```

Build on the target CPU with the release preset. Record the AMI, instance type,
CPU model, compiler, and resulting binary hash. `-march=native` means the binary
must not be copied to a different CPU family without rebuilding.

Create an unprivileged `bar-mm` user, keep the checkout under
`/opt/trading-algos`, and mount encrypted persistent storage at `/data/bar-mm`.
The service requires outbound HTTPS/WSS to `fapi.binance.com` and
`fstream.binance.com`; it needs no inbound public port and no Binance key.

Copy `infra/systemd/bar-portion-mm.service` to `/etc/systemd/system`, copy
`config.example.env` to `/etc/bar-portion-mm.env`, set the public-data shadow
parameters, restrict that file to root, and adjust paths if needed. The example
contains no credentials and the executable has no trading credential inputs.
Then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now bar-portion-mm
curl -s http://127.0.0.1:8060/health/ready
journalctl -u bar-portion-mm -f
```

Pin ingest, decision, recorder, and control work only after measuring actual
thread behavior. Isolate the decision CPU from routine interrupts where the
instance and kernel allow it; confirm with latency histograms rather than
assuming a tuning change helped. Locking memory or real-time scheduling needs a
separate minimal capability review.

Run a 72-hour continuous shadow capture, fault drills, replay, and the release
benchmark on this host. AWS-host results are the acceptance evidence; cloud
development-container results are informative only.
