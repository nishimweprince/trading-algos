# ta-core

The scaffolding every Python service in this repository repeated by hand before
the restructure: an HTTP-shaped error type, structured JSON logging with a
durable JSONL sink, a settings base, a FastAPI app factory that pre-wires
authentication and error handling, and the argparse/`.env.<profile>` bootstrap.

Extracted from `ctrader-markets`, which was the more evolved of the two copies —
its `JsonlLogger` has the crash-safe retry and `chmod 0600` that `mt5-trader`'s
lacked.

Nothing broker-specific belongs here.
