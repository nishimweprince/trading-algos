# PumpSwap replay research

The migration sniper is gone; pump-desk now runs curve-in, migrate-out (see the
top-level README). This directory is
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

## Curve-in, migrate-out replay

Every curve pump-desk admitted while incomplete, from admission to completion
(or 15 minutes when it never completed), plus the first minute of its pool:

```bash
cd ~/trading-algos
set -a; . pump-desk/.env; set +a
python3 pump-desk/research/fetch_curve_paths.py \
  --db pump-desk/data/pump-desk.db \
  --output pump-desk/research/data/curve-paths-v2.json.gz
python3 pump-desk/research/replay_curve.py \
  --input pump-desk/research/data/curve-paths-v2.json.gz \
  --report-output pump-desk/reports/curve-in-migrate-out.md
```

Schema 2 also fetches each curve's history from its create up to the path,
which costs one more paged query per curve (`--holder-max-pages`, default 60).
Schema 1 rows in an old `.partial.jsonl` are fetched again. The replay rebuilds
every wallet's net curve tokens at the decision point. The **overhang** is the
top-K holders' tokens (`--overhang-top-k`, default 10, matching
`curve.overhang_top_k`), and its drop is the price fall if they sold them all on
the curve. The report adds:

- **Overhang diagnostic**: the live rule's trades bucketed by drop at
  decision. If the hypothesis holds, the stall rate climbs with the drop.
- **Overhang off vs on**: the best train rule of each kind, with its test
  result.
- How many test entries the overhang vetoed, and how many paths had a
  truncated holder history (unknown, so vetoed by any finite threshold).

The selected rule may carry `OH<=X%/topK`. Only if the gate passes, set
`curve.overhang_max_drop_pct = X` in `pump-desk.toml`. Holder balances here come
from curve trades only; the live read also sees wallet-to-wallet transfers.

Curve reserves come from pump.fun `TradeEvent`s (event-CPI inner instructions,
else `Program data:` logs), so each point carries the fee actually charged. The
replay buys a fixed size once completion costs at most T SOL, lands it after
every transaction of the landing slot, sells on the pool X % up or N slots
after the migrate, and sells back on the curve when it stalls. Curves that never
completed stay in the sample: they are the stall losses.

## Tests

```bash
cd pump-desk/research
python3 -m unittest -v test_replay.py test_replay_curve.py
```
