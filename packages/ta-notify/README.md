# ta-notify

The notification-service client. Before the restructure it existed four times —
in `session-hedging`, `ipda`, `mt5-trader` and `lookup-trader` — and
`ipda/src/notifier.py` opened by saying it "mirrors the contract mt5-trader
already speaks". The copies were deliberate, documented, and exactly the kind of
thing one package should own.

This is the union of the best of them:

- the shared `httpx.AsyncClient` from session-hedging, rather than a fresh
  client per call
- the profile-suffixed `source` from ipda, so `ipda.forex` and `ipda.deriv` are
  distinguishable at the receiving end

The contract that must not change: **`send` never raises.** A notification
failure must not propagate into a trading path. Every copy stated this rule; the
tests here enforce it.

`NotificationSettings` is a mixin for a service's own settings class, carrying
the five `NOTIFICATION_*` environment fields and the channel validation.
