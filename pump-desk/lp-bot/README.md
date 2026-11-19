# pd-lp: live LP on fresh PumpSwap pools

> **Status (2026-11-19): paused.** 4 live positions, 0 profitable, −0.178 SOL (−18 % per position on average).
> The KILL file is in place on the VM and the wallet was emptied by its owner. See
> `../reports/feeds-and-lp-2026-11-19.md`.

`bot.mjs` (Node 20, `@pump-fun/pump-swap-sdk` 2.2.0) provides liquidity on the canonical PumpSwap pool of each
non-mayhem, SOL-quoted pump.fun migration:

1. **Signal:** `logsSubscribe` on the migration authority. The `CompletePumpAmmMigrationEvent` (mint, pool) is
   decoded from the migrate tx in about 0.4 s.
2. **Entry:** `ENTRY_DELAY_MS` (5 s) after the migrate. Buy the token with half of `SIZE_SOL`, then deposit token + SOL.
   The bot waits until the bought tokens and the minted LP are visible (RPC nodes can lag a confirmation) and
   never sends a zero-sized deposit.
3. **Hold:** the position is marked every second: LP share of both vaults, the token sold into the pool, plus the
   lamports parked in its own token/LP/WSOL accounts.
4. **Exit:** after `HOLD_MS` (5 min), or on a `STOP_PCT` (8 %) loss. The exit is **one tx**: withdraw + sell
   (leftover + the withdraw's minimum token out) + close the LP account, about 1,140 of 1,232 bytes. Any dust is
   sold and the token account closed afterwards.

## Risk controls

- **`KILL` file:** `DATA_DIR/KILL` stops new entries. Exits still run.
- **Position limits:** at most `MAX_OPEN` positions. `MIN_RESERVE_SOL` is never spent.
- **Daily loss limit:** `DAILY_LOSS_SOL` of realized loss per UTC day latches entries off until the next UTC day.
  A position already in flight can overshoot it.
- **Restarts:** open positions persist in `state.json` and are exited on restart.
- **P&L:** P&L per position is the wallet's SOL change over that position's own txs (fees, tips and rent included),
  written to `positions.jsonl`.

## Deploy (jito FRA VM)

- **Service:** systemd `pd-lp` runs as user `jito-arb` on cores 0–1.
- **Env:** `EnvironmentFile=/etc/jito-arb/jito-arb.env` provides `HELIUS_API_KEY`.
  `WALLET_PATH=/etc/pd-lp/wallet.json`, `DATA_DIR=/var/lib/pd-lp`.
- **Install:** `npm install` in a build directory, then copy it to `/opt/pd-lp`.

Never commit the wallet file. Never open it in an editor while chatting with an assistant: the selection is sent
as context.
