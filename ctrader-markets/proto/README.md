# Vendored cTrader Open API protobuf schemas

Source: <https://github.com/spotware/openapi-proto-messages>
Commit: `3fd8bddfbe0cfc2ecfda079623dc4e498af11e66` (2025-11-13)
License: see [LICENSE](LICENSE) (MIT, Spotware)

These four `.proto` files are vendored verbatim. Do not edit them.

## Why vendored rather than depending on `ctrader-open-api`

The published `ctrader-open-api` package ships the same schemas as pre-generated `_pb2` modules, but
it is unusable here for two independent reasons:

1. It hard-pins `protobuf==3.20.1`, which has no wheels for Python 3.11+. That pin is not something
   a dependent can relax — pip fails to resolve the environment outright.
2. Its `__init__.py` imports the Twisted-based client, so importing any of its message modules drags
   in a second event-loop framework that this service never runs.

Generating from source removes both problems, and pins the schema to a specific reviewed commit
instead of to whatever a third-party release happens to bundle.

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
