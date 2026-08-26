#!/usr/bin/env bash
# Run the Phase 3.3 verification matrix before or after frozen-service removal.
set -euo pipefail

mode="${1:-}"
if [[ "$mode" != "pre-delete" && "$mode" != "post-delete" ]]; then
  echo "usage: infra/verify_cutover.sh <pre-delete|post-delete>" >&2
  exit 64
fi

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

packages=(
  ta-core
  ta-contracts
  ta-store
  ta-notify
  ta-clients
  execution-service
  backtesting-service
)

for package in "${packages[@]}"; do
  if [[ -d "packages/${package}" ]]; then
    directory="packages/${package}"
  else
    directory="services/${package}"
  fi
  (
    cd "$directory"
    uv run --package "$package" ruff check .
    uv run --package "$package" ruff format --check .
    uv run --package "$package" pytest -m "not integration"
  )
done

npm run notifications:test
npm run docs:build

uv run --package backtesting-service \
  python services/backtesting-service/scripts/determinism_gate.py \
  services/backtesting-service package

.venv/bin/ruff check infra/cutover_audit.py infra/tests/test_cutover_audit.py
.venv/bin/ruff format --check infra/cutover_audit.py infra/tests/test_cutover_audit.py
.venv/bin/python -m pytest -q infra/tests/test_cutover_audit.py

if [[ "$mode" == "pre-delete" ]]; then
  [[ -d mt5-trader ]] || {
    echo "error: mt5-trader must exist for the pre-delete frozen-service check" >&2
    exit 1
  }
  (
    cd mt5-trader
    ./.venv/bin/python -m pytest -m "not integration"
  )
else
  [[ ! -e mt5-trader ]] || {
    echo "error: mt5-trader still exists during the post-delete check" >&2
    exit 1
  }
  [[ ! -e .github/workflows/mt5-trader-ci.yml ]] || {
    echo "error: the obsolete mt5-trader CI workflow still exists" >&2
    exit 1
  }
fi
