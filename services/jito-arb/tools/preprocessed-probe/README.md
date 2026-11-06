# SubscribePreprocessed availability gate

Build separately from the service (requires protoc, grpc_cpp_plugin, grpc++,
protobuf and Abseil development packages):

```sh
bash services/jito-arb/tools/preprocessed-probe/build.sh /tmp/preproc-probe
```

Run `/tmp/preproc-probe/probe` with `PREPROC_ENDPOINT` (TLS endpoint) and
`PREPROC_TOKEN` supplied through a protected environment, and `PREPROC_ACCOUNTS_FILE` naming a newline-separated list of tracked pool addresses. Empty filters are refused. Never pass credentials as command-line
arguments. The probe opens exactly one stream, subscribes to non-vote
transactions, answers pings, and terminates after 60 seconds. No reconnect or
alternate transport is attempted. It does not subscribe to accounts.

Exit 0 requires the full observation window, at least one transaction with a
64-byte signature and transaction message, no malformed transaction envelopes,
and the expected deadline status. Exit 1 is a failed availability gate; exit 2
means missing configuration. `grpc_code` uses canonical gRPC status codes:
7 permission denied, 8 resource exhausted, 12 unimplemented, 16 unauthenticated.
Server error text is deliberately suppressed to avoid leaking credentials.

This executable links only generated protobuf/gRPC code. It has no dispatcher,
transaction sender, recorder or mutable engine state. It writes only aggregate
counts to stdout and does not create kind-1 records or feed recordings.

Do not enable or implement shadow integration on the strength of a failed gate.
Account updates must continue using the standard account stream if the gate
eventually succeeds.

## Gate result — 2026-11-06

Base: `feat/jito-arb-coverage` at `01f61484`.
Standalone executable compiled successfully on the VM under
`/tmp/jito-preproc-probe-01f61484`, using the pinned repository proto.
The probe used the running service's effective `LASERSTREAM_ENDPOINT` override
and its existing credential through a private environment (neither printed).

```text
probe_success=0 elapsed_ms=0 grpc_code=12 transactions=0 malformed=0 pings=0
probe_exit=1
arb_feed_reconnects 0
arb_feed_shards_up 5
```

The endpoint returned **UNIMPLEMENTED**, ending the intended 60-second probe
immediately. The availability gate failed. Per the requested stop condition,
client integration, transaction/ALT parsing, overlays, matching, recording and
dashboard integration are deferred. There was no WebSocket fallback, deployment,
transaction send, service restart or server checkout modification.

An earlier attempt against the config-file placeholder (before resolving the
service's environment override) returned UNAVAILABLE, code 14; that was not the
effective-endpoint result. Before and after the effective-endpoint attempt, the
existing service had five active streams and zero reconnects (PID 224987).

Filtered repeat on 2026-11-06:

```text
filter_accounts=1047 rate_per_s=0 created_at_lag_us_p50=-1 created_at_lag_us_p99=-1 probe_success=0 elapsed_ms=1 grpc_code=12 transactions=0 malformed=0 pings=0
probe_exit 1
```

The required pool filter was applied. The same immediate `UNIMPLEMENTED` response
blocks Phase 6; no shadow implementation or transport substitution is enabled.
