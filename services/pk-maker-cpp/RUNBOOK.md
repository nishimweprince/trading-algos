# pkm runbook

The host is Dublin (Azure North Europe today; see `infra/systemd/README.md`
for the AWS eu-west-1 move). It is the closest place Polymarket lets us trade
from: the CLOB's origin is AWS eu-west-2 (London), and the UK is close-only.

## Keys

`/etc/pkm.env`, owned by root, `chmod 600`. Start from `config.example.env`.

| Variable | What |
|---|---|
| `POLY_PRIVATE_KEY` (or `POLY_PRIVATE_KEY_FILE`) | The key that owns the deposit wallet |
| `POLY_FUNDER` | The deposit wallet's address |
| `POLY_RELAYER_API_KEY` | Without it, settled winners are not redeemed and the pUSD stays locked |
| `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` | Read-only Kalshi market data |
| `PKM_LIVE_CONFIRM=yes` | The live guard |

CLOB API credentials are derived from the key at each start unless
`POLY_CLOB_API_*` are set.

## Build and install (once, then on each upgrade)

```sh
cd ~/trading-algos/services/pk-maker-cpp
cmake --preset release && cmake --build --preset release && ctest --preset release
sudo cp ../../infra/systemd/pkm.service /etc/systemd/system/   # edit User=/paths for the host (Dublin: poly)
sudo systemctl daemon-reload
```

The Dublin host runs units as `poly` from `/home/poly/trading-algos`. Install
the unit with the same `sed` as the other units there:

```sh
sed -e 's/^User=pk$/User=poly/' -e 's/^Group=pk$/Group=poly/' -e 's#/home/pk/#/home/poly/#g' \
  ../../infra/systemd/pkm.service | sudo tee /etc/systemd/system/pkm.service >/dev/null
```

## Cutover from pk-polymaker (once)

The two must never quote the same wallet.

1. `sudo systemctl disable --now pk-polymaker`. Its shutdown cancels
   everything and reads the final order states.
2. Carry its book over, so open inventories still settle, redeem and count
   toward today's loss limit:

   ```sh
   mkdir -p data/state
   cp ../poly-kalshi-service/data/state/dev/polymaker/state.json data/state/state.json
   ```

   pkm reads pk-polymaker's state file as is: inventories, tracked orders,
   realised P&L by day, halt and ack.
3. Check the order path. `check` posts one post-only bid five ticks under the
   best bid at the minimum size (at most $3) and cancels it at once. It fails
   if anything matched.

   ```sh
   sudo -u poly build/release/pkm --env /etc/pkm.env check
   ```

4. `sudo systemctl enable --now pkm`, then watch the log:
   `journalctl -u pkm -f`.
5. On `pk-status`, the "Maker" tab reads pk-polymaker's API, which is now
   gone. Read results from the wallet instead.

**First day:** run at minimum exposure: `PK_PM_SHARES=5` and
`PK_PM_MAX_OPEN_COST_USD=20`.

## Start, stop, upgrade

```sh
sudo systemctl start pkm
sudo systemctl stop pkm        # pulls quotes, cancel-all, final order states, state.json
git pull && cmake --build --preset release && sudo systemctl restart pkm
```

Each start cancels everything on the account before it quotes.

## Kill switch and halt

- **Kill:** `touch data/KILL` (`PKM_KILL_FILE`).
  - Within 250 ms every quote is pulled and a cancel-all is sent.
  - The process stays up, with feeds and fills still running.
  - `rm data/KILL` resumes quoting.
- **Halt:** realised P&L today at or below −`PK_PM_DAILY_LOSS_USD`, or the
  wallet holding fewer shares than booked on three checks in a row, latches a
  halt.
  - The halt is persisted in `state.json` and cancels everything.
  - It lasts until the next UTC day. To clear it earlier:
    `touch data/state/ACK`. The loss limit then counts from that moment.
- **Dead man:** if the hot loop stalls for 2 s, the control thread sends a
  cancel-all over its own connection.

## Reading the log

`journalctl -u pkm`:
- One `status:` line a minute: each coin's gate (`quoting`, `pause`,
  `no fair`, …) and its resting bids, plus posts, cancels, fills, rejects, the
  pre-sign hit rate, realised P&L today and open cost.
- One line per event: each fill, settlement, redemption, reject, drift, halt
  and reconnect.

## Reviewing performance

```sh
journalctl -u pkm -o cat | python3 scripts/pkm_review.py data/state/state.json --since <deploy time, ISO UTC>
```

It scores settled windows (P&L per window with its t) and fills split into
first and pairing legs by asset, price, quote age and time in the window, with
window-clustered standard errors. Judge a config only after about 350 windows of
it (`--since` its deploy), and only on t near 2: at 125 windows the 10-27
review's −$24.56 was t −0.93, noise. A taker second leg to "lock" a pair was
tested and rejected that day: after an adverse first fill, buying the other side
at market fixes the loss in place and pays the spread and the taker fee
(0.07·p·(1−p) per share).

## Failure modes

| Symptom | Cause / action |
|---|---|
| `CLOB protocol version is 3, not 2` at start | Polymarket moved the order protocol. Regenerate the golden vectors from the new SDK, port the struct, and do not force it |
| `signed empty POST /order (want 400)` | L2 credentials rejected: clear `POLY_CLOB_API_*` so they are derived again, or check the key |
| `status:` shows `feeds stale` | No Kalshi or Polymarket frame for 5 s. Reconnects are automatic; a Kalshi seq gap reconnects with a fresh snapshot |
| `no fair` for a long time | Kalshi's book is wide (> 2¢) or older than 10 s. This is normal during Kalshi's Thursday 03:00–05:00 ET closure |
| `reject … not enough balance` | Cash is waiting on redemptions. Bids the free balance cannot cover are held for 3 s at a time. Check `POLY_RELAYER_API_KEY` |
| `orphan order … cancelled` | A post whose answer was lost rested on the venue untracked. Two reconcile passes in a row (30 s apart) must see it untracked before it is cancelled |
| Posts rejected over the order's timestamp | A pre-signed order is up to `PKM_PRESIGN_TTL_MS` (5 s) old when sent. Lower it; at 0 every post is signed inline (~35 µs) |
| `status:` gate `quoting:spot-up-off` / `spot-down-off`, `spot_pauses N` | Coinbase moved ≥ `PK_PM_SPOT_PAUSE_BPS` within 1 s: a fall holds the Up first leg off, a rise the Down one, for `PK_PM_SPOT_PAUSE_MS`. Pairing bids stay. A climbing count in calm markets means the threshold is too tight |
| `spot closed …: reconnecting`, no `spot connected` | The Coinbase feed is down: the guard and `smove_bps` tags stop, quoting goes on (Kalshi alone). `PKM_SPOT_WS_URL=off` runs without it |
| Turning on widening or reprice | After 1–2 days of tags, `pkm_review.py --since <spot deploy>` must show "spot against" fills worse than "flat". Then `PK_PM_SPOT_WIDEN_PER_BP=0.002`, a day later `PK_PM_JUMP_MODE=reprice` (needs `(bN)` on the coin in `status:`: Kalshi's learned β), one at a time, each judged a day on the same split |
| `stray … the slot already holds …`, `strays N` in `status:` | An order rested that its slot no longer tracks (a post answered after the slot moved on, or an order from `state.json`). The engine cancels it, retrying each second until its final state is known. A steady climb means a slot race: read the log around the first one |
| `reject … crosses book` / `… marketable BUY …` | Our view of the ask was stale. The slot waits 500 ms and bids at most a tick under the refused price for 2 s |
| `pkm-alert: … no destination` in the journal | pkm stopped uncleanly and no alert went out. Set `PK_TELEGRAM_BOT_TOKEN` and `PK_TELEGRAM_ADMIN_USER_IDS` in `/etc/pkm.env` |
| `drift … streak 3` → halt | The wallet holds fewer shares than booked. Compare with polymarket.com, fix `state.json` by hand if a fill was booked wrongly, then `ACK` |
| `redeem failed` 20 times | The relayer refuses. Redeem on polymarket.com; the engine stops trying after 20 attempts |
