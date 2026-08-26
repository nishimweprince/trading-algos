#!/usr/bin/env bash
# Regenerate src/execution_service/adapters/ctrader/_generated/ from the vendored proto/ schemas.
#
# The generated modules are committed, so this only needs running when the
# vendored .proto files change. See proto/README.md.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
OUT="src/execution_service/adapters/ctrader/_generated"

if ! "$PYTHON" -c "import grpc_tools.protoc" 2>/dev/null; then
  echo "grpcio-tools is missing. Install the dev extra:" >&2
  echo "  $PYTHON -m pip install -e '.[dev]'" >&2
  exit 1
fi

rm -f "$OUT"/*_pb2.py "$OUT"/*_pb2.pyi
mkdir -p "$OUT"

"$PYTHON" -m grpc_tools.protoc \
  --proto_path=proto \
  --python_out="$OUT" \
  --pyi_out="$OUT" \
  proto/*.proto

# The schemas declare no protobuf package and import each other by bare
# filename, so protoc emits flat `import OpenApiX_pb2` statements that only
# resolve if the output directory is itself on sys.path. Rewrite them to
# package-relative imports so the modules work as a normal subpackage.
for generated in "$OUT"/*_pb2.py; do
  perl -pi -e 's/^import (OpenApi\w+_pb2) as /from execution_service.adapters.ctrader._generated import $1 as /' \
    "$generated"
done

echo "Generated into $OUT:"
ls -1 "$OUT"/*_pb2.py
