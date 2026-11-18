# Lead-time probe (shadow)

How much earlier does Helius `preprocessedSubscribe` (pre-execution transactions, WebSocket) show a transaction on
our pools than LaserStream gRPC at `processed` (what the bot trades on)? Both feeds are filtered to the same
accounts and joined by signature in one process, so the lead is measured on one clock. Read-only: nothing is
signed or sent, and nothing is shared with the bot. It uses one LaserStream connection (the bot uses 9 of 10)
and one WebSocket connection; 0.1 credits per pre-execution message.

```sh
bash services/jito-arb/tools/lead-probe/build.sh /tmp/lead-probe
# env from a protected file, never on the command line: HELIUS_API_KEY, LASERSTREAM_ENDPOINT
LEAD_ACCOUNTS_FILE=/tmp/lead-accounts.txt LEAD_SECONDS=1800 LEAD_OUT=/tmp/lead.json /tmp/lead-probe/probe
```

`lead_ms` is the gRPC arrival minus the WebSocket arrival for the same signature: positive means the pre-execution
feed was earlier. `ws_only` are transactions the processed stream never reported (failed or dropped before
execution); `grpc_only` are ones the best-effort pre-execution feed missed.
