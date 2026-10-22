# Changelog

Deploying agent: apply the **Deploy** steps of the newest entry before starting `live`. Entries are newest first.

## Quote floor, margin gating, hedge price capture (2026-10-22)

The first live canary on the Windows box (`D:\hlarb`) lost **−$1.93 of $40.81 in about 80 minutes**. Only `xyz:SP500` traded: 46 fills, $22.9k notional.

| Cost | $ |
|---|---|
| Fill price vs fair at the fill | −0.86 |
| HL fees (HIP-3 maker is **+30 ppm**, a cost, not a rebate) | −0.71 |
| MT5 hedge paying half the spread, 30 hedges | −0.60 |

How it happened:
- **Quotes sat at fair ±1 tick, about 13 ppm on US500.** The touch-join added in ec0abe6 overrode the ~121 ppm half-spread.
- **The 50 ppm per lot-step inventory skew pushed the unwind quote through fair.** The position then flipped ±0.1 every 10–60 s, paying fees and the MT5 spread each time.
- **Places for the four other pairs were rejected 981 times with "Insufficient margin".** One lot step there is ~$1k of notional on a $20 HL account. The rejects used up the HL request budget (`requests_remaining` reached 0).
- **`HLA_HL_MIN_MARGIN_USD=20` equalled the deposit**, so the first loss soft-halted the bot.
- **MT5 hedges came back with `px:0, deal:0`**, so hedge cost could not be measured.

### Changes

1. **Cost floor.**
   - No quote may sit closer to fair than `cost + HLA_MIN_EDGE_PPM`, where `cost = HL maker + HLA_MT5_COMMISSION_PPM + MT5 half-spread + HLA_ADVERSE_PPM`.
   - Joining the HL touch can tighten a quote from `cost + HLA_EDGE_PPM` down to that floor, never further.
   - New key: `HLA_MIN_EDGE_PPM`, default 10.
   - The status page and the `f` journal lines show `floor_ppm` next to `half_spread_ppm`.
2. **The skew can't cross the floor.** It is still applied, but the floor is measured from the unskewed fair, so skew only widens a side.
3. **Margin gating.**
   - A flat pair whose quote can't be margined is blocked: `block: "margin"`. The check is free HL margin vs `notional / min(HLA_HL_LEVERAGE, max leverage) × 1.1`.
   - With a position open, only the side that reduces it is quoted.
   - An "Insufficient margin" reject parks the pair for `HLA_MARGIN_PARK_S` (new key, default 600) instead of retrying every 1–30 s.
   - Rejected acks now journal `"ok":false,"post_ok":<request accepted>`. They used to say `"ok":true`.
4. **Margin floors** belong well below the deposit. See the env changes below. No code change.
5. **MT5 hedge price.**
   - When the broker returns DONE without a deal, `HlaExecutor` (v1.10) looks the deal up by order ticket in history, for up to `DealLookupMs` = 100 ms. If it still can't find it, it uses the request price.
   - `txn` journal lines gain `px_src` (0 = result, 1 = deal history, 2 = request price) and `slip_ppm` (fill vs the MT5 touch when the hedge was sent; positive is a cost).
   - `hlarb report` adds `hedges_priced`, `mt5_slip_ppm_mean` and `mt5_commission_per_hedge_usd`.
   - The bridge layout is unchanged: `px_src` travels in the high byte of the txn status.
6. **Stale quotes.**
   - A resting quote that fair has moved inside the floor is moved at once, ignoring `HLA_MIN_REQUOTE_MS` and `HLA_REQUOTE_PPM`.
   - The default for `HLA_REQUOTE_PPM` is now 10 (was 30).
7. **Hedgeable sizes.** After a partial fill, the side that reduces the position quotes only the part beyond whole MT5 lot steps. The position returns to whole steps, so the hedge can match it.

Drills added: `quote_floor`, `skew_floor`, `margin_block`, `margin_reject_park`, `stale_quote_inside_floor`, `partial_fill_resize`. `quotes_and_hedge` had been failing since ec0abe6 and now passes with floor prices.

### Deploy

1. **Stop the bot and flatten.**
   - Stop `hlarb.ps1` and create `D:\hlarb\hlarb.kill`.
   - Flatten `xyz:SP500` on HL and `US500` on MT5 by hand if either is open. At the last check they were HL −0.08, MT5 +0.1 lots.
   - Delete the kill file.
2. **Pull and build.** Pull branch `fix/hlarb-quote-floor`, or `main` once it is merged. In MSYS2 CLANG64, run `cmake --build --preset release -j && ctest --preset release`. All 3 ctest entries must pass.
3. **Copy the build outputs.**
   - Copy `build/release/hlarb.exe` to `D:\hlarb\`.
   - Copy `build/release/hla_bridge.dll` to `MQL5\Libraries\`. Rebuild and copy both even though the layout did not change: they must come from the same commit.
4. **Update the executor (the MQL5 changed).**
   - Copy `mt5/HlaExecutor.mq5` to `MQL5\Services\` and compile it in MetaEditor. It must compile with 0 errors and show version 1.10.
   - Restart the HlaExecutor service. HlaPublisher is unchanged.
5. **Edit `D:\hlarb\hlarb.env`.** Change these lines and leave the rest:

   ```
   HLA_PAIRS=xyz:SP500=US500
   HLA_EDGE_PPM=30
   HLA_ADVERSE_PPM=40
   HLA_MIN_EDGE_PPM=10
   HLA_REQUOTE_PPM=10
   HLA_MAX_INVENTORY_LOTS=2
   HLA_HL_MIN_MARGIN_USD=5
   HLA_MT5_MIN_MARGIN_USD=5
   HLA_MARGIN_PARK_S=600
   ```

   - JPY, EUR, SILVER and XYZ100 stay out until the HL account holds at least ~$150. With less, `block: "margin"` would park them anyway.
   - Keep `HLA_MT5_COMMISSION_PPM=35` until `hlarb report` shows the real `mt5_commission_per_hedge_usd`, then set it from that.
6. **Check.** Run `powershell -ExecutionPolicy Bypass -File D:\hlarb\hlarb.ps1 check`. It must exit 0, which means hedged.
7. **Dry run.** Run `hlarb.ps1 dry-run` for at least 2 h with both services running, then `D:\hlarb\hlarb.exe report D:\hlarb\journal\<dry-run journal>.jsonl`. Move on only if all of these hold:
   - no halts;
   - every `f` line for `xyz:SP500` has `floor_ppm` ≈ 120–160, and a resting `bid` ≤ fair × (1 − floor_ppm/1e6), `ask` ≥ fair × (1 + floor_ppm/1e6);
   - no `"Insufficient margin"` ack more than once per 10 minutes.
8. **Go live** with `hlarb.ps1 live`.

### Verify after going live

- **Status page** (https://hyperliquid.nishimweprince.dev/): SP500 `Half/floor ppm` reads about 150/130, `block` is `none`, and `Budget left` climbs back above 500.
- **First hedge:** the `txn` line has `px` > 0. `px_src` should be 0 or 1; 2 means the deal lookup failed, so note it. `slip_ppm` should be present.
- **After 1 h:** run `hlarb report` on the live journal and record `markout_1s_ppm`, `mt5_slip_ppm_mean`, `mt5_commission_per_hedge_usd` and `equity_change_usd`.
- **Stop rule:** if 4 h live shows net equity down with at least 20 fills, create the kill file, flatten, and report the numbers above. Don't retune and restart.
- **If nothing fills in 4 h:** the floor is wider than the market. Report it. Don't lower `HLA_MIN_EDGE_PPM` or `HLA_ADVERSE_PPM` without sign-off.
