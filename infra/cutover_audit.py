#!/usr/bin/env python3
"""Read-only Phase 3.3 cutover audit.

The report intentionally omits credentials, raw broker account IDs, operation IDs,
broker order IDs, prices, and payload bodies. It is safe to attach to cutover evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import dotenv_values

WINDOW_SECONDS = 24 * 60 * 60
ANOMALY_EVENTS = {
    "access_token_invalidated",
    "access_token_rejected",
    "ctrader_client_disconnected",
    "ctrader_connect_failed",
    "ctrader_connection_closed",
    "ctrader_environment_connect_failed",
    "ctrader_reauth_required",
    "ctrader_server_error",
    "event_handling_failed",
    "forced_close_failed",
    "gateway_event_handling_failed",
    "heartbeat_failed",
    "notification_failed",
    "proactive_token_refresh_failed",
    "refreshing_invalidated_token",
    "service_stopping",
    "stream_subscriber_lagging",
    "symbol_resolution_failed",
}


def parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp lacks a timezone: {value}")
    return parsed.astimezone(UTC)


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def launchd_snapshot(label: str) -> dict[str, Any]:
    result = run(["launchctl", "print", f"gui/{run(['id', '-u']).stdout.strip()}/{label}"])
    if result.returncode != 0:
        return {"loaded": False}
    fields: dict[str, Any] = {"loaded": True}
    wanted = {
        "state": "state",
        "program": "program",
        "working directory": "working_directory",
        "stdout path": "stdout_path",
        "stderr path": "stderr_path",
        "runs": "runs",
        "pid": "pid",
        "last terminating signal": "last_terminating_signal",
    }
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*([^=]+?)\s*=\s*(.*?)\s*$", line)
        if not match or match.group(1) not in wanted:
            continue
        key = wanted[match.group(1)]
        if key in fields:
            continue
        value: Any = match.group(2)
        if key in {"runs", "pid"}:
            value = int(value)
        fields[key] = value
    fields["production_profile_argument"] = bool(
        re.search(r"(?m)^\s*--profile\s*$\n^\s*production\s*$", result.stdout)
    )
    return fields


def listeners(port: int) -> list[dict[str, Any]]:
    result = run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpcn"])
    if result.returncode not in {0, 1}:
        raise RuntimeError(f"lsof failed with exit code {result.returncode}")
    found: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in result.stdout.splitlines():
        if not line:
            continue
        prefix, value = line[0], line[1:]
        if prefix == "p":
            current = {"pid": int(value)}
            found.append(current)
        elif current is not None and prefix == "c":
            current["command"] = value
        elif current is not None and prefix == "n":
            current["address"] = value
    return found


def read_jsonl(
    path: Path, start: datetime, end: datetime, timestamp_fields: tuple[str, ...]
) -> tuple[list[dict[str, Any]], int, int]:
    rows: list[dict[str, Any]] = []
    parse_errors = 0
    missing_timestamps = 0
    if not path.is_file():
        return rows, parse_errors, missing_timestamps
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        raw_timestamp = next((row.get(key) for key in timestamp_fields if row.get(key)), None)
        if raw_timestamp is None:
            missing_timestamps += 1
            continue
        try:
            timestamp = parse_instant(str(raw_timestamp))
        except ValueError:
            parse_errors += 1
            continue
        if start <= timestamp <= end:
            rows.append(row)
    return rows, parse_errors, missing_timestamps


def log_summary(rows: list[dict[str, Any]], parse_errors: int, missing: int) -> dict[str, Any]:
    levels = Counter(str(row.get("level", "UNKNOWN")).upper() for row in rows)
    events = Counter(str(row.get("event", "UNKNOWN")) for row in rows)
    anomalies = Counter()
    for row in rows:
        event = str(row.get("event", "UNKNOWN"))
        if event in ANOMALY_EVENTS:
            anomalies[event] += 1
        if event == "trade_operation_completed" and str(row.get("state", "")).lower() in {
            "failed",
            "rejected",
            "unknown",
        }:
            anomalies[f"trade_operation_{str(row.get('state')).lower()}"] += 1
        if event == "service_error_response":
            status = int(row.get("status_code", 0) or 0)
            if status >= 500:
                anomalies["service_5xx_response"] += 1
    return {
        "records": len(rows),
        "parse_errors": parse_errors,
        "missing_timestamps": missing,
        "levels": dict(sorted(levels.items())),
        "events": dict(sorted(events.items())),
        "anomalies": dict(sorted(anomalies.items())),
    }


def stderr_summary(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False, "lines": 0, "suspicious_lines": 0}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    suspicious = re.compile(r"\b(error|warning|failed|exception|traceback|critical)\b", re.I)
    return {
        "present": True,
        "lines": len(lines),
        "suspicious_lines": sum(bool(suspicious.search(line)) for line in lines),
    }


def fetch_json(url: str, api_key: str | None = None) -> tuple[int, dict[str, Any]]:
    headers = {"X-API-Key": api_key} if api_key else {}
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def summarize_accounts(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    by_environment = Counter(str(account.get("environment")) for account in accounts)
    return {
        "count": len(accounts),
        "by_environment": dict(sorted(by_environment.items())),
        "all_connected": all(bool(account.get("connected")) for account in accounts),
        "all_reconciled": all(bool(account.get("reconciled")) for account in accounts),
        "all_full_access": all(
            account.get("broker_access_rights") == "full_access" for account in accounts
        ),
        "all_available_for_trading": all(
            bool(account.get("available_for_trading")) for account in accounts
        ),
        "all_order_entry_enabled": all(
            bool(account.get("order_entry_enabled")) for account in accounts
        ),
        "all_position_close_enabled": all(
            bool(account.get("position_close_enabled")) for account in accounts
        ),
    }


def runtime_snapshot(service_dir: Path, port: int, now: datetime) -> dict[str, Any]:
    env = dotenv_values(service_dir / ".env.production")
    api_key = env.get("API_KEY")
    base_url = f"http://127.0.0.1:{port}"
    live_status, live = fetch_json(f"{base_url}/health/live")
    ready_status, ready = fetch_json(f"{base_url}/health/ready")
    trading_status, trading = fetch_json(f"{base_url}/health/trading-ready")
    accounts_status, accounts = fetch_json(f"{base_url}/v1/accounts", api_key)
    tick_status, tick = fetch_json(
        f"{base_url}/v1/market-data/tick?{urlencode({'symbol': 'XAUUSD'})}", api_key
    )
    tick_timestamp = tick.get("ts")
    tick_age = None
    if tick_timestamp:
        tick_age = (now - parse_instant(str(tick_timestamp))).total_seconds()

    ready_details = ready.get("details") or {}
    trading_details = trading.get("details") or {}
    health_accounts = trading_details.get("accounts") or ready_details.get("accounts") or {}
    reconnects: dict[str, int] = {}
    for item in health_accounts.values():
        environment = str(item.get("environment", "unknown"))
        reconnects[environment] = max(
            reconnects.get(environment, 0), int(item.get("reconnects", 0))
        )

    return {
        "health_live": {"http_status": live_status, "status": live.get("status")},
        "health_ready": {"http_status": ready_status, "status": ready.get("status")},
        "health_trading_ready": {
            "http_status": trading_status,
            "status": trading.get("status"),
            "database_healthy": trading_details.get("database_healthy"),
            "trading_enabled": trading_details.get("trading_enabled"),
            "live_trading_enabled": trading_details.get("live_trading_enabled"),
        },
        "reconnects_by_environment": dict(sorted(reconnects.items())),
        "accounts": {
            "http_status": accounts_status,
            **summarize_accounts(accounts.get("accounts") or []),
            "unconfigured_authorized_accounts": accounts.get("unconfigured_authorized_accounts"),
            "unavailable_authorized_accounts": accounts.get("unavailable_authorized_accounts"),
        },
        "xauusd_tick": {
            "http_status": tick_status,
            "provider": tick.get("provider"),
            "timestamp": tick_timestamp,
            "age_seconds": round(tick_age, 3) if tick_age is not None else None,
            "bid_present": tick.get("bid") is not None,
            "ask_present": tick.get("ask") is not None,
            "spread_nonnegative": tick.get("spread", -1) >= 0,
        },
    }


def scalar(connection: sqlite3.Connection, query: str) -> Any:
    return connection.execute(query).fetchone()[0]


def grouped(connection: sqlite3.Connection, query: str) -> dict[str, int]:
    return {str(key): int(value) for key, value in connection.execute(query).fetchall()}


def ledger_snapshot(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        return {
            "integrity_check": scalar(connection, "PRAGMA integrity_check"),
            "operations_total": scalar(connection, "SELECT COUNT(*) FROM operations"),
            "operation_states": grouped(
                connection, "SELECT state, COUNT(*) FROM operations GROUP BY state"
            ),
            "target_states": grouped(
                connection, "SELECT state, COUNT(*) FROM operation_targets GROUP BY state"
            ),
            "target_errors": scalar(
                connection,
                """SELECT COUNT(*) FROM operation_targets
                   WHERE error_code IS NOT NULL OR error_message IS NOT NULL
                      OR lower(state) IN ('failed', 'rejected', 'unknown')""",
            ),
            "failed_operations": scalar(
                connection,
                """SELECT COUNT(*) FROM operations
                   WHERE lower(state) IN ('failed', 'rejected', 'unknown')""",
            ),
            "duplicate_operation_ids": scalar(
                connection,
                """SELECT COUNT(*) FROM (
                       SELECT operation_id FROM operations GROUP BY operation_id HAVING COUNT(*) > 1
                   )""",
            ),
            "duplicate_payload_groups": scalar(
                connection,
                """SELECT COUNT(*) FROM (
                       SELECT source, payload_hash FROM operations
                       GROUP BY source, payload_hash HAVING COUNT(*) > 1
                   )""",
            ),
            "execution_event_types": grouped(
                connection,
                "SELECT event_type, COUNT(*) FROM execution_events GROUP BY event_type",
            ),
        }
    finally:
        connection.close()


def acceptance(
    report: dict[str, Any], expected_pid: int | None, expected_runs: int | None
) -> dict[str, Any]:
    launchd = report["supervision"]["unified_launchd"]
    expected_paths = report["supervision"]["expected_paths"]
    runtime = report["runtime"]
    durable = report["logs"]["durable"]
    console = report["logs"]["console"]
    stderr = report["logs"]["stderr"]
    ledger = report["ledger"]
    listener_pids = {item["pid"] for item in report["supervision"]["port_8010_listeners"]}
    checks = {
        "minimum_24_hours": report["window"]["elapsed_seconds"] >= WINDOW_SECONDS,
        "unified_launchd_running": launchd.get("loaded") and launchd.get("state") == "running",
        "expected_pid": expected_pid is None or launchd.get("pid") == expected_pid,
        "expected_run_count": expected_runs is None or launchd.get("runs") == expected_runs,
        "workspace_binary_and_profile": launchd.get("program") == expected_paths["program"]
        and launchd.get("production_profile_argument") is True,
        "production_working_directory": launchd.get("working_directory")
        == expected_paths["working_directory"],
        "production_log_paths": launchd.get("stdout_path") == expected_paths["stdout_path"]
        and launchd.get("stderr_path") == expected_paths["stderr_path"],
        "sole_expected_listener": len(listener_pids) == 1 and launchd.get("pid") in listener_pids,
        "legacy_launchd_unloaded": not report["supervision"]["legacy_launchd"].get("loaded"),
        "ready": runtime["health_ready"]["http_status"] == 200
        and runtime["health_ready"]["status"] == "ready",
        "trading_ready": runtime["health_trading_ready"]["http_status"] == 200
        and runtime["health_trading_ready"]["status"] == "ready",
        "database_healthy": runtime["health_trading_ready"]["database_healthy"] is True,
        "expected_account_inventory": runtime["accounts"]["count"] == 3
        and runtime["accounts"]["by_environment"] == {"demo": 2, "live": 1}
        and runtime["accounts"]["unconfigured_authorized_accounts"] == 0
        and runtime["accounts"]["unavailable_authorized_accounts"] == 1,
        "accounts_reconciled": runtime["accounts"]["all_connected"]
        and runtime["accounts"]["all_reconciled"]
        and runtime["accounts"]["all_full_access"]
        and runtime["accounts"]["all_available_for_trading"]
        and runtime["accounts"]["all_order_entry_enabled"]
        and runtime["accounts"]["all_position_close_enabled"],
        "zero_reconnects": runtime["reconnects_by_environment"] == {"demo": 0, "live": 0},
        "quote_current": runtime["xauusd_tick"]["http_status"] == 200
        and runtime["xauusd_tick"]["age_seconds"] is not None
        and 0 <= runtime["xauusd_tick"]["age_seconds"] < 60,
        "no_durable_log_parse_errors": durable["parse_errors"] == 0
        and durable["missing_timestamps"] == 0,
        "no_console_log_parse_errors": console["parse_errors"] == 0
        and console["missing_timestamps"] == 0,
        "no_error_or_warning_events": durable["levels"].get("ERROR", 0) == 0
        and durable["levels"].get("WARNING", 0) == 0
        and console["levels"].get("ERROR", 0) == 0
        and console["levels"].get("WARNING", 0) == 0,
        "no_stderr_failures": stderr["present"] and stderr["suspicious_lines"] == 0,
        "no_classified_anomalies": not durable["anomalies"],
        "ledger_integrity": ledger["integrity_check"] == "ok",
        "no_failed_operations": ledger["failed_operations"] == 0 and ledger["target_errors"] == 0,
        "no_duplicate_operations": ledger["duplicate_operation_ids"] == 0
        and ledger["duplicate_payload_groups"] == 0,
    }
    return {"passed": all(checks.values()), "checks": checks}


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    repo = args.repo.resolve()
    service_dir = repo / "services/execution-service"
    start = parse_instant(args.start)
    end = parse_instant(args.end) if args.end else datetime.now(UTC)
    if end < start:
        raise ValueError("end must not precede start")

    env = dotenv_values(service_dir / ".env.production")
    database_value = env.get("EXECUTION_DATABASE_PATH", "data/executions.production.sqlite3")
    database_path = service_dir / str(database_value)
    events_value = env.get("EVENTS_LOG_PATH", "logs/events.production.jsonl")
    events_path = service_dir / str(events_value)
    console_path = service_dir / "logs/production.log"
    stderr_path = service_dir / "logs/production.error.log"

    durable_rows, durable_errors, durable_missing = read_jsonl(
        events_path, start, end, ("ts", "timestamp")
    )
    console_rows, console_errors, console_missing = read_jsonl(
        console_path, start, end, ("timestamp", "ts")
    )
    report: dict[str, Any] = {
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "elapsed_seconds": round((end - start).total_seconds(), 3),
        },
        "supervision": {
            "unified_launchd": launchd_snapshot("com.execution-service.production"),
            "legacy_launchd": launchd_snapshot("com.ctrader-markets.production"),
            "port_8010_listeners": listeners(args.port),
            "expected_paths": {
                "program": str(repo / ".venv/bin/execution-service"),
                "working_directory": str(service_dir),
                "stdout_path": str(service_dir / "logs/production.log"),
                "stderr_path": str(service_dir / "logs/production.error.log"),
            },
        },
        "runtime": runtime_snapshot(service_dir, args.port, end),
        "logs": {
            "durable": log_summary(durable_rows, durable_errors, durable_missing),
            "console": log_summary(console_rows, console_errors, console_missing),
            "stderr": stderr_summary(stderr_path),
        },
        "ledger": ledger_snapshot(database_path),
    }
    report["acceptance"] = acceptance(report, args.expected_pid, args.expected_runs)
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--start", required=True, help="UTC/offset observation-window start")
    result.add_argument("--end", help="UTC/offset window end; defaults to now")
    result.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    result.add_argument("--port", type=int, default=8010)
    result.add_argument("--expected-pid", type=int)
    result.add_argument("--expected-runs", type=int)
    result.add_argument(
        "--require-acceptance",
        action="store_true",
        help="exit 1 unless every 24-hour acceptance check passes",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        report = build_report(args)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"cutover audit failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.require_acceptance and not report["acceptance"]["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
