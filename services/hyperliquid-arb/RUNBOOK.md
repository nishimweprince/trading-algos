# Runbook: hlarb on the Windows box

Who this is for: the operator setting up and running the live canary. The formulas are in [`STRATEGY_SPEC.md`](STRATEGY_SPEC.md). The host choice and Phase 0 measurements are in [`docs/aws-tokyo-windows.md`](docs/aws-tokyo-windows.md).

## 1. Box

1. Launch an AWS **ap-northeast-1** **c7i.2xlarge** instance with **Windows Server 2022**, in the AZ chosen in Phase 0. Use gp3 storage and an Elastic IP.
2. Make RDP reachable from your IP only. No other inbound rules.
3. Power plan: run `powercfg /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c` (High Performance).
4. Time: check `w32tm /query /status`. The AWS Time Sync source is `169.254.169.123`.
5. Add a Defender exclusion for `C:\hlarb`. Set Windows Update active hours, and patch only while flat.
6. Auto-logon for the trading user (Sysinternals Autologon). The terminal and hlarb run in that user's session, because the bridge is a `Local\` mapping.

## 2. Build (MSYS2 CLANG64)

```sh
pacman -S --needed mingw-w64-clang-x86_64-{clang,cmake,ninja,curl,openssl} git
curl-config --protocols | grep -i wss          # must list WSS
cmake --preset release && cmake --build --preset release -j && ctest --preset release
```

`ctest` runs every unit test, the signing vectors and `hlarb drill`. Then:

- copy `build/release/hlarb.exe` and the CLANG64 runtime DLLs it needs (`ldd hlarb.exe | grep clang64`) to `C:\hlarb\`;
- copy `build/release/hla_bridge.dll` (statically linked) to `<terminal data folder>\MQL5\Libraries\`;
- copy `mt5/HlaBridge.mqh`, `HlaPublisher.mq5` and `HlaExecutor.mq5` to `MQL5\Services\`, and compile both in MetaEditor;
- copy `scripts/hlarb.ps1` to `C:\hlarb\`, and `config.example.env` to `C:\hlarb\hlarb.env`;
- create `C:\hlarb\journal\` and `C:\hlarb\secrets\`.

Build `hlarb.exe` and `hla_bridge.dll` from the same commit. The bridge layout is versioned, and a mismatch refuses to open.

## 3. MT5 terminal

- Log in to the broker account (netting mode preferred; hedging works).
- Tools → Options → Expert Advisors: **Allow algorithmic trading** and **Allow DLL imports**.
- Add every pair's symbol to Market Watch, with names exactly as in `HLA_PAIRS`.
- Navigator → Services: add **HlaPublisher** and **HlaExecutor**, keep the default `BridgeName`, and tick *Allow algo trading* for the executor. Start them; they wait for hlarb.
- The executor's `Magic` (7750001) marks hlarb's positions. Never trade those symbols by hand with that magic.

## 4. Hyperliquid

- Approve an API (agent) wallet for the trading account at app.hyperliquid.xyz → API. Save its private key to `C:\hlarb\secrets\agent.key`, and restrict the file's ACL to the trading user. Never put the master key on the box.
- **Margin depends on account abstraction.** On **Unified** / **Portfolio margin**, spot USDC collateralizes `xyz:*` (and hlarb reads spot). On **Standard / Manual**, HIP-3 margin is per dex: transfer spot → `xyz` via Transfers / `sendAsset` (agents cannot move funds). Prefer Standard for high-frequency quoting (no 50k action/day cap). `hlarb check` prints `hl_min_withdrawable`.
- Fund HL and MT5 so each has margin for `HLA_MAX_GROSS_NOTIONAL_USD` at `HLA_HL_LEVERAGE`, plus the floors.

## 5. Configure, check, dry-run, go live

1. Fill in `C:\hlarb\hlarb.env`: account, key file, logins, and the broker's exact symbol names.
2. Run `powershell -ExecutionPolicy Bypass -File C:\hlarb\hlarb.ps1 check`. It is read-only and prints one JSON summary per pair: asset id, growth mode, maker ppm, MT5 step, value per point, HL units per lot step, positions. It exits 0 only if everything is hedged. Check by eye:
   - `hl_units_per_lot_step × price` is about the notional you intend per quote;
   - `maker_ppm` matches the HL fee screen for that market.
3. Run `hlarb.ps1 dry-run` for **24 h**, with the services running. Quotes, acks, fills and hedges are simulated, and nothing is sent. Then run `hlarb report C:\hlarb\journal\*.jsonl`: there must be no halts, and the quotes must be where you expect.
4. Run `hlarb.ps1 live`. For a restart at logon, create a Task Scheduler task at logon that runs the same command.

## 6. Monitoring

- `C:\hlarb\hlarb-status.json`, rewritten every second, shows:
  - the halt and soft halt;
  - per pair: block, fair, quotes, HL and MT5 positions, `unhedged_lots`;
  - the request budget and equities.
- The journal is one JSON object per line:

| `k` | Content |
|---|---|
| `start` / `stop` | run summary and positions |
| `f` | per-pair snapshot each second |
| `fill` | HL fill with fair and MT5 quotes at that moment |
| `hedge` / `txn` | hedge sent and result (`hedge_ms`: HL fill → MT5 result) |
| `halt` / `soft_halt` / `pull` | risk state changes |
| `flatten` / `flatten_ack` | emergency IOC |
| `equity` | HL + MT5 equity each second |

- `hlarb report <journals>` gives fills, fees, hedge latency p50/p99, and 1 s / 10 s markouts per coin. Negative markouts mean adverse selection: widen `HLA_ADVERSE_PPM`.

## 7. Stopping and incidents

| Situation | Action |
|---|---|
| Planned stop | Ctrl-C in the console (or end the task). Quotes are pulled, open orders are swept, and hedged positions stay open. |
| Stop now, from anywhere | Create `C:\hlarb\hlarb.kill`. Quotes are pulled within 500 ms. |
| Kill file written by hlarb | Read it: each line is the time and the reason. Reconcile both venues by hand until `hlarb check` shows `"hedged":true`, then delete the file and start. |
| `emergency` | A hedge failed twice or went missing. hlarb sent one IOC to flatten the HL residual. Check the `flatten_ack`, the MT5 positions with magic 7750001, and HL positions. Square them by hand, then follow the row above. |
| `forced_fill` | ADL or liquidation on HL. The fill was hedged. Find out why (margin, HL incident) before restarting. |
| `position_mismatch` | A venue's position differs from hlarb's. Do not restart until you know why: a manual trade, a lost fill, or a broker action. |
| Box or terminal crash | HL `scheduleCancel` cancels every order within `HLA_DEADMAN_S`. Positions stay hedged unless a fill landed in the gap; `check` will show that. |
| Weekend | FX and CFD sessions close, and those pairs block as `mt5_closed`. Crypto pairs keep quoting only if the broker's crypto CFDs trade then. |

## 8. Broker conduct

- Get written confirmation from the broker that hedged positions opened by an EA, held for seconds to minutes, are acceptable.
- Watch for message caps (Pepperstone states about 30,000 server messages a day per EA). Each hedge is one or two messages.
- Requotes, slippage asymmetry or spread widening on your account show up as `txn` retcodes and in `hedge_ms` and markouts. Review the report weekly.
