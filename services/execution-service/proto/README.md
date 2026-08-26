# Vendored cTrader Open API protobuf schemas

Source: <https://github.com/spotware/openapi-proto-messages>
Commit: `3fd8bddfbe0cfc2ecfda079623dc4e498af11e66` (2025-11-13)
License: see [LICENSE](LICENSE) (MIT, Spotware)

These four `.proto` files are vendored verbatim. Do not edit them.

## Why vendored rather than depending on `ctrader-open-api`

The published `ctrader-open-api` package ships the same schemas as pre-generated `_pb2` modules.
It does install on Python 3.12 — its `protobuf==3.20.1` pin resolves via that release's pure-Python
`py2.py3-none-any` wheel — but it is still unusable here:

1. Every dependency is an exact pin (`Twisted==24.3.0`, `pyOpenSSL==24.1.0`, `protobuf==3.20.1`,
   `requests==2.32.3`, `inputimeout==1.0.4`) and a dependent cannot relax any of them. That means
   `cryptography==42.0.8` can never be patched, and the protobuf pin forces the **pure-Python**
   implementation — no `upb` accelerator — on a tick-rate decode path.
2. Its `__init__.py` imports the Twisted-based client, so importing any message module drags in a
   second event-loop framework this service never runs. `--no-deps` does not escape it either: the
   generated modules cross-import each other as `from ctrader_open_api.messages import ...`, which
   re-triggers that `__init__`.
3. Its last release (0.9.3) is yanked; the newest usable one is 0.9.2, dated 2024-06-26.

Generating from source removes all of it, and pins the schema to a specific reviewed commit instead
of to whatever a third-party release happens to bundle. The generated code itself is
forward-compatible — protoc-3.20 output loads fine on the protobuf 6.x runtime — so the cost is only
the `generate_protos.sh` step, not a protocol rewrite.

## Regenerating

```bash
.venv/bin/python -m pip install -e '.[dev]'
./scripts/generate_protos.sh
```

Output goes to `src/ctrader/_generated/` and **is committed** — the build does not require
`protoc`, only the `protobuf` runtime. Regenerate only when bumping the vendored schemas, and commit
the `.proto` change and the regenerated modules together.

## Upgrading the schemas

```bash
curl -sL https://github.com/spotware/openapi-proto-messages/archive/<sha>.tar.gz | tar xz --strip=1 \
  -C proto '*/*.proto' '*/LICENSE'
./scripts/generate_protos.sh
.venv/bin/pytest tests/test_proto.py
```

Update the commit hash at the top of this file. `tests/test_proto.py` is the canary: it asserts the
payload-type registry populates and that the message classes this service depends on still exist
with the field names it reads.
