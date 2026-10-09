# Running the zone-trend research on a VM

This guide gets the M4 verdict on a VM: download the data, build the universe, then run the walk-forward, ablations and holdout. Don't run it on a laptop. The full-universe run downloads tens of GB and takes hours of CPU.

`DEPLOY.md` is the separate guide for running the bot. The research VM can be the same host later, but the bot only needs a small one.

## 1. VM

| | Recommended | Why |
|---|---|---|
| Region | **Tokyo (ap-northeast-1)** | The Binance Vision bucket is in Tokyo. Downloads are much faster and time out less. |
| OS | Ubuntu 24.04 LTS | |
| CPU | 8 vCPU (4 works, about twice as slow) | Tape builds and walk-forward runs are parallel processes |
| RAM | 16 GB | Each worker holds a few symbol tapes and a month of minutes |
| Disk | **100 GB** data disk, mounted at `/data` | See estimates below |

The universe union is about 70 coins (2020-01 to today). Disk and time estimates:

| Stage | Disk | Time (8 vCPU, Tokyo) |
|---|---|---|
| 1. Daily klines, every USDT perpetual | < 1 GB | ~30–60 min (≈650 symbols × 80 months of small files) |
| 2–3. Hourly pool + full history for the union | ~25 GB with zips, ~12 GB with `ZT_DROP_RAW=1` | ~1–3 h |
| 4. Validate | — | ~10 min |
| 5. Walk-forward (54 configs × ~70 tapes, 756 in-sample runs, OOS variants, robustness) | ~2 GB of tape cache | ~3–6 h |
| 6. Ablations (6 components) | small | ~1 h |

## 2. One-time setup

```bash
sudo apt-get update && sudo apt-get install -y git tmux sqlite3
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.local/bin/env
```

```bash
git clone https://github.com/nishimweprince/trading-algos.git ~/trading-algos
```

```bash
cd ~/trading-algos && git checkout feat/zone-trend && uv sync --package zone-trend --group dev --python 3.12
```

Put the data on the big disk:
```bash
sudo mkdir -p /data/zone-trend && sudo chown "$USER" /data/zone-trend
```

```bash
echo 'export ZT_DATA_DIR=/data/zone-trend' >> ~/.bashrc && source ~/.bashrc
```

Sanity check. The tests use synthetic data only, so they need no download:
```bash
cd ~/trading-algos/services/zone-trend && uv run --package zone-trend pytest -q -m "not integration"
```

## 3. Run it

Use tmux so an SSH drop doesn't kill the run:
```bash
tmux new -s zt
```

```bash
cd ~/trading-algos/services/zone-trend && ZT_DROP_RAW=1 ops/research_pipeline.sh all
```

- **Detach** with `Ctrl-b d`; **reattach** with `tmux attach -t zt`.
- **Progress** goes to `logs/research-*.log`, e.g. `tail -f logs/research-*.log`.
- **If it stops** (network, reboot), run the same command again. Finished months and cached tapes are skipped.
- **Stages** can also be run one at a time: `ops/research_pipeline.sh data`, then `wf`, then `ablate`.
- **Options:**
  - `ZT_FROM` / `ZT_TO` change the period. The default is 2020-01 through the last full month, with the last 9 months held back as the holdout.
  - `ZT_WORKERS` caps the processes.

When stage 5 finishes, it prints the acceptance table, and `runs/wf-<from>-<to>/` holds the stitched OOS trades and equity, plus the 2× slippage, delay and benchmark variants, the robustness runs and `acceptance.json`.

## 4. Look at the results

The dashboard reads the `runs/` folder. Start it on the VM (loopback only):
```bash
cd ~/trading-algos/services/zone-trend && uv run --package zone-trend zt status --runs-dir runs --port 8060
```

From your laptop, open a tunnel and browse to http://localhost:8060/research:
```bash
ssh -N -L 8060:127.0.0.1:8060 <user>@<vm-ip>
```

To keep a copy, the results without the data are a few MB:
```bash
rsync -av <user>@<vm-ip>:trading-algos/services/zone-trend/runs/ services/zone-trend/runs/
```

## 5. Decide

1. Read the acceptance table. The spec is explicit: **if the strategy fails the benchmark test, do not deploy it.**
2. If the walk-forward passes, run the holdout **once**. It refuses a second run without `--force`, because a second look turns it into in-sample data:
   ```bash
   uv run --package zone-trend zt holdout --wf runs/wf-2020-01-<to>
   ```
   ```bash
   uv run --package zone-trend zt accept --wf runs/wf-2020-01-<to>
   ```
3. Label golden episodes on a couple of symbols, then re-run the golden tests:
   ```bash
   uv run --package zone-trend zt episodes --symbol BTCUSDT --from 2023-01 --to 2024-12
   ```
   Fill in `episodes/BTCUSDT/labels.yaml`, copy it to `tests/golden/labels.yaml`, and run `pytest tests/golden`.
4. Only then go on to `DEPLOY.md` (shadow first).

## 6. Clean up

Keep `runs/`. The data can be deleted and rebuilt (`rm -rf /data/zone-trend/raw` alone frees most of it if you didn't use `ZT_DROP_RAW`). If the VM was only for research, snapshot or delete it.

## Notes
- **TradFi contracts are excluded.** Binance lists tokenised stocks and metals (XAUUSDT, TSLAUSDT, …) as USDT perpetuals. `universe.coins_only: true` (the default) keeps them out of the universe; set it to false to study them.
- **Exchange filter history.** Vision has no history of exchange filters or leverage brackets. The backtests use the newest snapshot and a pessimistic 1% maintenance margin, and record that in every run's metadata.
- **Download retries.** If downloads keep timing out, lower `ZT_WORKERS` (the bucket throttles). Failed series are listed and retried on the next run.
