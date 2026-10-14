# PumpSwap replay research

The migration sniper is shelved. The VM keeps `pump-desk/KILL` present, so the
process can collect migrations without opening new positions. This directory is
the reusable research path for testing long-only PumpSwap ideas before any live
implementation.

## 1. Fetch on the guarded VM

The fetcher reads the Helius URL from the VM environment and writes only compact
reserve paths. It uses Helius `getTransactionsForAddress` with an ascending,
time-bounded query, so it does not walk a pool's entire history.

```bash
cd ~/trading-algos
set -a; . pump-desk/.env; set +a
python3 pump-desk/research/fetch_pool_paths.py \
  --db pump-desk/data/pump-desk.db \
  --output pump-desk/research/data/pool-paths.json.gz
```

The adjacent `.partial.jsonl` file makes interrupted runs resumable. Neither it
nor the compressed path dataset is committed; both stay on the VM and never
contain the RPC URL or key. Pools that still have a pagination token after the
configured `--max-pages` limit are marked as truncated and excluded from replay.

## 2. Replay on the VM

```bash
python3 pump-desk/research/replay.py \
  --input pump-desk/research/data/pool-paths.json.gz \
  --json-output pump-desk/reports/pumpswap-adjacent-ideas.json \
  --report-output pump-desk/reports/pumpswap-adjacent-ideas.md
```

The screener searches delayed momentum, breakout, dip/rebound and post-self-pump
entries on the chronological training prefix. It reports only the untouched
test suffix as evidence. Primary selection excludes mayhem and non-WSOL pools
because their fee/quote models have not been independently verified.

## Tests

```bash
cd pump-desk/research
python3 -m unittest -v test_replay.py
```
