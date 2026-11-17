# Going live (Stage 4)

This build no longer refuses `dry_run = false`. Live starts only when every precondition holds, and the switch
itself stays in `/etc/jito-arb/bot.toml` on the VM: deploying this code changes nothing while that file says
`dry_run = true`.

## Preconditions (checked at start; any missing one refuses with exit code 2)

- the hot wallet file (`keys.hot_wallet_path`, mode 0600), and it is not the dry-run test key
- `keys.executor_program_id` set (deployed 17 Nov 2026: 5DHkkjNCixT2e7Sh89bv5aYtCJmjrJxWKeW3mLFwarGq)
- `mode.armed_path` readable (enforced live: routes through unarmed pools are skipped)
- `mode.alts_path` with at least one table on chain (live never compiles against virtual tables)
- `jito.regions`, and `flash.reserves` in flash mode

## Checklist

1. `arm-accounts ... --send` has finished (8 tables, 1,878 addresses) and `alts.json` is copied to the backup folder.
2. Telegram: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `/etc/jito-arb/jito-arb.env`. The bot starts without them,
   but then a feed outage only reaches the journal.
3. Deploy this build: `git pull`, `cmake --build --preset vm`, `sudo services/jito-arb/deploy/install.sh`. The unit no
   longer passes `--dry-run`; the config decides.
4. Add the Stage 3 limits to `/etc/jito-arb/bot.toml` (install.sh never overwrites it):
   - `[engine]`: `max_trade_lamports = 2000000000`, `max_bundles_per_slot = 1`
   - `[risk]`: `max_daily_fee_spend_sol = 0.02`, `min_sol_float = 0.3`
5. Set `dry_run = false` in `[mode]`, then `sudo systemctl restart jito-arb`. The journal shows `LIVE: ...` with the limits.
6. Watch: `journalctl -u jito-arb -f`, the dashboard, `arb_bundles_sent` / `arb_bundles_landed` / `arb_bundles_rejected`.

## Stop

- At once: `sudo -u jito-arb touch /var/lib/jito-arb/KILL` (sending stops, feeds keep running; remove the file to resume).
- Fully: `dry_run = true` and restart.

## Stop conditions for the test (docs/LIVE-READINESS.md)

Any landed loss, any guard mismatch, or a landing rate under 2% after 500 bundles. Exit criteria: at least 20 landed
bundles, realized output within 1 unit of the quote, kill switch and limits exercised live.
