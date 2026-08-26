# ta-clients

Typed clients for this platform's own HTTP services, so a caller does not
hand-roll paths, headers and response classification.

`ExecutionClient` is extracted from `session-hedging/src/execution.py`. Two
things in it are load-bearing:

- **A transport failure returns `UNKNOWN`, never `REJECTED`.** The order may or
  may not have reached the broker. The caller reconciles; it must not resubmit.
  Note that this is the exact opposite of `ta-notify`'s never-raise contract —
  there a dropped message is acceptable, here a silently dropped order is the
  worst outcome available.
- **`OPERATION_NAMESPACE` is fixed forever.** Operation ids are `uuid5`-derived
  from it so that a restart mid-submit recomputes the same id and the gateway's
  idempotency recognises the retry. Change it and every in-flight operation
  looks new, opening a second position where a retry was intended.

`decimal_text` is likewise not cosmetic: the gateway hashes the submitted
payload to detect a reused `operation_id` with a changed body, so `1E-2` and
`0.01` are two different requests to it.

The market-data client (`CandleStore`) lands here when backtesting-service
migrates; it is currently still coupled to session-hedging's path configuration.
