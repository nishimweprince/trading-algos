# ofi-scalper-service

The OFI scalper in `.plans/ofi-scalper-plan.md`: Binance USDⓈ-M BTCUSDT/ETHUSDT
order books kept verified, every raw frame recorded, the feature engine on a
100 ms grid, a deterministic regime gate, hard risk with a kill switch, and the
hot path **model → policy → risk → execution bridge** (Phase C).

Orders need two things: an execution mode that sends or simulates them, and a
pinned model whose research gates passed. Until research produces one, the
service records and computes features; testnet can run "controls only" for the
kill, dead-man and flatten drills. `OFI_EXECUTION_MODE=live` is refused.

Port 8030. Data comes from `ta-plugin-binance-futures` through
`load_providers` (the plugin's `streams`, `rest` and `account` factory extras).

## Run

```sh
uv sync --package ofi-scalper-service --group dev
cp services/ofi-scalper-service/.env.example.dev services/ofi-scalper-service/.env.dev
# fill in API_KEY, the risk limits, optionally the read-only key and the bot token
.venv/bin/ofi-scalper-service --profile dev
curl -s localhost:8030/health/ready | jq .details
curl -s -H "X-API-Key: $KEY" localhost:8030/v1/status | jq
curl -s -H "X-API-Key: $KEY" 'localhost:8030/v1/features?symbol=BTCUSDT' | jq
```

Under launchd: `infra/launchd/install.sh --service ofi-scalper-service dev`.

## Execution modes

| `OFI_EXECUTION_MODE` | Needs | Does |
|---|---|---|
| `off` (default) | nothing | Records and computes features; a halt only logs `ofi_would_act` |
| `shadow` | `OFI_MODEL_VERSION` with a passing `gates.json` | Scores every grid sample, runs the policy and risk, builds every order, and fills them in a pessimistic simulator (a maker order needs a trade printed *through* its price). Nothing is sent |
| `testnet` | `EXECUTION_API_KEY`, execution-service on `BINANCE_FUTURES_ENV=testnet` | Sends orders to execution-service (`ADAPTERS=binance_futures`), priced from demo trading's own touch; fills are polled back. Without a model: controls only |
| `live` | refused | Mainnet waits for a binding gates pass and explicit approval (Stage 5) |

- **Model** (`model.py`): `<OFI_MODEL_DIR>/<version>/` with a manifest holding
  the sha256 of every file. Any mismatch, an unknown feature, or a `gates.json`
  that did not pass refuses to load. The model's `engine.json` (PCA weights,
  microprice table) configures the feature engine; its `policy` block sets the
  horizon, threshold, barrier, entry timeout and time stop. Install the scorer
  with `--extra model`.
- **Policy** (`policy.py`, pure, shared with research): entry when calibrated
  `p ≥ threshold × gate multiplier` and `(p − p_opposite) × barrier ≥ 2 × maker
  fee + buffer`, post-only at the touch; cancel on timeout, decay or a block;
  reduce-only post-only take-profit at the barrier; reduce-only market close on
  the opposite barrier (watched on the mid) or at `horizon × k`. Fixed size
  (`OFI_ORDER_NOTIONAL_USD`); Kelly sizing stays off until calibration on our
  own fills passes.
- **Bridge** (`execution_bridge.py`): every order passes `RiskState.check_order`
  first. UNKNOWN outcomes are reconciled through the gateway, never resubmitted;
  `OFI_MAX_CONSECUTIVE_UNKNOWN` in a row halts. A halt (kill switch, loss
  limits, execution fault) cancels all, closes every cycle reduce-only, then
  flattens as a backstop. In testnet the dead-man (`countdownCancelAll`,
  `OFI_DEAD_MAN_MS`) is re-armed every 5 s, state survives restarts in
  `<OFI_STATE_DIR>/bridge.json`, and at startup a position the bridge did not
  open halts without flattening, for a human to resolve.
- **Records** (`trades.py`): `<OFI_STATE_DIR>/trades/` (one line per cycle:
  fills, fees, funding, P&L, exit reason, mid move 1/5/30 s after the fill,
  order RTT) and `signals/` (every threshold crossing and what was done).
  `ofi-daily-check` adds the day's trading report to its Telegram summary.

### Testnet drills without a model (controls only)

```sh
# execution-service-binance running on demo trading; in .env.dev:
#   OFI_EXECUTION_MODE=testnet, EXECUTION_API_KEY=<the gateway's API_KEY>
sudo systemctl restart ofi-scalper-service
curl -s -H "X-API-Key: $KEY" localhost:8030/v1/status | jq .bridge   # venue_ready, dead_man_armed
# place a small resting order on the gateway (POST localhost:8010/v1/orders), then:
touch ~/trading-algos/services/ofi-scalper-service/data/KILL.dev   # cancel-all, then flatten
sudo systemctl kill -s KILL ofi-scalper-service                    # dead-man: Binance cancels
```

## Book modes

`BINANCE_FUTURES_BOOK_MODE` picks how the order book arrives:

| Mode | Stream | Book | Needs |
|---|---|---|---|
| `diff` | `<s>@depth@0ms` + REST snapshots | full depth, every event | a link that keeps up (Tokyo) |
| `partial` | `<s>@depth10@100ms` | complete top-N snapshot each 100 ms | a few KB/s per symbol |

In `partial` mode a late or missed frame only makes the book older; nothing
breaks or resyncs. What changes in the features: OFI is computed between
consecutive 100 ms snapshots (intra-interval events net out), and
`depth_*_{5,10}bp` sum only the N levels the snapshot carries. Research must
use recordings made in the same mode, which a replay of the raw files
guarantees. Measured on the Mac link (2026-10-01): diff depth and bookTicker
fell seconds behind and kept falling; `depth10@100ms` held a steady ~250 ms.

## Routes

| Route | Auth | |
|---|---|---|
| `GET /health/live` | no | process up |
| `GET /health/ready` | no | both stream routes connected and every book verified |
| `GET /v1/status` | yes | books, risk (halt, pauses, limits), gate, recorder, fees, counts, feed latency |
| `GET /v1/features?symbol=` | yes | latest feature vector (grid or trade-burst sample) |
| `GET /v1/signals?limit=` | yes | recent threshold crossings, newest first |
| `GET /v1/trades?date=&limit=` | yes | recent cycles (or one UTC day's) and their summary |
| `GET /dashboard` | page | signals, cycles, policy state and today's report, live; asks for the key |
| `POST /v1/kill` | yes | halt (cancel all, close, flatten); body `{"reason": "..."}` optional |
| `POST /v1/kill/ack` | yes | clear a halt; refused while the kill file exists; daily-loss halts wait for the next UTC day |

Kill switch, three ways: `touch data/KILL.dev`, `POST /v1/kill`, or `/kill`
to the scalper's own Telegram bot from an admin id. Acknowledging is HTTP-only.

## How a sample is taken

Events are consumed in local-receive order. Before an event stamped `t` is fed,
every 100 ms grid time `g <= t` is sampled (`state_engine.GridClock`), so a
sample sees exactly the events before `g`, which is what a replay of the
recording sees. `MarketState.sample` raises `LookaheadError` if asked for a
time at or before an event already fed. Depth diffs are applied to the book as
the consumer dequeues them, so the book always matches the events seen.

## Recording

`data/raw/<profile>/<SYMBOL>/<YYYYMMDD>/<SYMBOL>_<YYYYMMDD>_<HH>.gz`, lines
`<recv_ns> <raw frame>`, plus a `.json` manifest per file (host, counts, gaps).
Depth snapshots are recorded as `<s>@depthSnapshot` frames. Validate with
`ofi-gapcheck data/raw/dev` (non-zero exit on backwards time or an unrecovered
depth break).

## Health on a headless host

- **Heartbeat:** every `OFI_HEARTBEAT_SECONDS` (default 60) one `ofi_heartbeat`
  log line: events/s per stream, depth and trade lag p50/p99, book states,
  gaps, reconnects, backlog, pauses, halted, recorder lines/s and disk free.
  The latest one is also in `/v1/status.heartbeat`.
- **Disk guard:** below `OFI_MIN_FREE_DISK_GB` the recorder closes its files,
  stops writing and alerts; it resumes at 120% of the floor. Everything else
  keeps running.
- **Key check:** at boot, `GET /sapi/v1/account/apiRestrictions` reports what
  the configured key itself may do (`/v1/status.key_check`); trading or
  withdrawal permission on it raises an alert. The account-level flags in
  `fees.account` (`canTrade`, `canWithdraw`) are the account's, not the key's.
- **Daily check:** `ofi-daily-check --profile dev [--date YYYY-MM-DD]` runs
  gapcheck over a UTC day and sends a per-symbol summary (hours, MB, breaks,
  trade gaps); `infra/systemd/ofi-daily-check.timer` runs it at 00:20 UTC.

On Linux, run it under systemd: see [infra/systemd/README.md](../../infra/systemd/README.md).

## Stage 2 research

`python -m research.pipeline --profile dev --provisional` trains, selects and
gates a model from the recordings; see `research/README.md`. The backtest runs
this service's own bridge, policy and risk on replayed recordings.

## Stage 0 tools

- `ofi-latency --profile dev --seconds 120`: clock offset, offset-corrected
  feed latency per stream, unsigned and signed REST RTT, fee tier and
  commission rates. Writes `research/latency.json`. Order RTT needs a trading
  key and is not measured. Mac numbers are tagged `host=mac` and are not valid
  for the research gates.
- `uv run python -m research.collect.vision coverage`: what data.binance.vision
  actually holds (see `research/README.md`).

## Layout

| Module | |
|---|---|
| `state_engine.py` | Features (plan §1.3), pure; research imports it |
| `regime_gate.py` | Deterministic gate: funding blackout, liquidation burst, vol, spread percentile |
| `risk.py` | Frozen limits, halts, pauses, order check, rate governor |
| `runtime.py` | The live loop and watchers |
| `model.py` | Pinned, hash-verified CatBoost model, calibrator, scorer |
| `policy.py` | Entry, exit and sizing state machine (plan §1.5), pure; research imports it |
| `execution_bridge.py` | Policy actions → risk → shadow simulator or execution-service |
| `trades.py`, `dashboard.py` | Trade and signal records, daily report, `/dashboard` |
| `recorder.py`, `gapcheck.py` | Raw feed recording and validation |
| `telegram_commands.py`, `alerts.py` | `/kill`, `/status` in; alerts out via notification-service |
| `latency.py` | Stage 0 measurement |
