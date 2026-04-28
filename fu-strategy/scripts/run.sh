#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT}"

if [[ -x "${ROOT}/.venv/bin/uvicorn" ]]; then
  UVICORN="${ROOT}/.venv/bin/uvicorn"
else
  UVICORN="uvicorn"
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

exec "${UVICORN}" app.main:app --reload --host "${HOST}" --port "${PORT}"
