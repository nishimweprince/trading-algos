# Bar Portion shadow-service runbook

## Normal start

1. Confirm no unintended `KILL` file exists.
2. Start the unit and inspect `journalctl -u bar-portion-mm`.
3. Require `/health/ready` to show `ready:true`, `mode:"shadow"`, and rising
   depth/trade counts.
4. Run `bar-mm gapcheck` against the active journal after the capture window.

Readiness requires a verified book, one closed candle, and no halt. Startup
connects the WebSocket before requesting the REST snapshot so depth updates are
buffered by the socket while the snapshot is fetched.

## Dashboard

Open `http://127.0.0.1:8060/` on the host for the operator dashboard. It is
loopback-only by design; from elsewhere use an SSH forward
(`ssh -L 8060:127.0.0.1:8060 <host>`) or a tunnel such as
`cloudflared tunnel --url http://127.0.0.1:8060`. The page polls
`/api/summary` and `/api/activity` every five seconds. Curves and fills come
from the current journal file only, so rotate-aware history needs the archived
journals; headline counters cover the process lifetime. If the page shows
`HALTED`, follow the kill and feed-gap sections below before trusting any quote
state.

## Kill and recovery

Create the configured kill file to stop new intents and cancel all simulated
orders. It remains effective across restart. Remove the file only after the
reason is understood; the service then acknowledges it after a verified book is
available. `bar-mm drill` checks sequence-gap pause/recovery and durable kill.

## Feed gap or reconnect

A `pu` discontinuity marks the book invalid, cancels shadow orders, and halts
quotes. Restart the process to reconnect and obtain a fresh snapshot. Do not
infer state across a gap. Validate the journal and compare the deterministic
replay hash before returning the capture to research.

## Journal or disk fault

Journal write failure stops the feed handler and leaves the process non-ready.
Keep at least 20% disk free, rotate by UTC day outside the hot path, and never
delete the current file while the service owns it. Archived journals are
research inputs and should retain checksums.

## Latency regression

Run `bar-mm benchmark` on an otherwise idle target host with the release build.
Capture CPU model, compiler, flags, kernel, p50/p99/p99.9/max, and allocation
count. If p99 exceeds 9 microseconds, keep shadow operation available but mark
the latency gate failed; inspect CPU migration, power state, interrupts,
frequency, thermal throttling, and recorder pressure before changing code.

## Live-order boundary

There is no operational live mode. The process rejects it and has no secret
settings. Enabling orders requires a separate reviewed change, reconciled user
stream, dedicated account, live-readiness report, explicit limits, and approval
under Phase 6 of the plan.
