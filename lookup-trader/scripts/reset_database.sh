#!/usr/bin/env bash
# Reset DuckDB labelling data. Usage:
#   ./scripts/reset_database.sh              # dry run
#   ./scripts/reset_database.sh --yes        # clear occurrences
#   ./scripts/reset_database.sh --full --yes # delete DB and re-bootstrap
#
# Stop ./scripts/dev.sh first — DuckDB locks the database file.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/scripts/reset_database.py" "$@"
