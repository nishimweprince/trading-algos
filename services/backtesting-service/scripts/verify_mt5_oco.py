"""Probe MT5 by default; --execute explicitly places bounded owned OCO tests.

Run from backtesting-service using the execution-service workspace package.
All test state stays in backtesting-service. The active profile files are unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from execution_service.adapters.mt5.legacy_repository import SignalRepository
from execution_service.adapters.mt5.mt5_adapter import RealMT5Adapter
from execution_service.adapters.mt5.oco_models import OcoGroupRequest
from execution_service.adapters.mt5.oco_repository import OcoRepository
from execution_service.adapters.mt5.oco_service import Mt5OcoService
from execution_service.adapters.mt5.service import SignalExecutionService
from execution_service.config import load_settings
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
REPORT = ROOT / "reports" / "mt5-oco-verification.json"


class CountingAdapter(RealMT5Adapter):
    def __init__(self) -> None:
        super().__init__()
        self.entry_requests = 0

    def order_send(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if request.get("action") == self.constants.trade_action_pending:
            self.entry_requests += 1
        return super().order_send(request)


def write_report(report: dict[str, Any]) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


async def settle(service: Mt5OcoService, group_id: UUID) -> dict[str, Any]:
    await service.close_owned_group(group_id)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        await service.monitor_once()
        group = await service.get(group_id)
        if all(
            leg["state"] in {"closed", "cancelled", "expired", "rejected", "not_submitted"}
            and leg.get("resting") is False
            for leg in group["legs"].values()
        ):
            if group.get("fault"):
                group = await service.acknowledge_recovery(group_id)
            return group
        await asyncio.sleep(0.25)
    raise RuntimeError(f"owned group {group_id} did not settle; inspect the retained ledger")


async def verify(args: argparse.Namespace, report: dict[str, Any]) -> None:
    os.chdir(ROOT.parent / "execution-service")
    settings = load_settings(args.profile)
    settings = settings.model_copy(
        update={
            "mt5_oco_enabled": True,
            "mt5_oco_server_utc_offset_seconds": args.server_offset_seconds,
        }
    )
    adapter = CountingAdapter()
    initialized = adapter.initialize(settings)
    if not initialized:
        raise RuntimeError("MT5 initialization failed")
    service: Mt5OcoService | None = None
    owned: list[UUID] = []
    try:
        account = adapter.account_metadata()
        report["account"] = {
            key: account.get(key)
            for key in ("login", "server", "currency", "margin_mode", "trade_mode", "trade_allowed")
        }
        if account.get("login") != settings.login or account.get("server") != settings.server:
            raise RuntimeError("MT5 account identity does not match the configured profile")
        symbols = sorted(settings.allowed_symbols)
        broker_symbol = args.symbol or (symbols[0] if len(symbols) == 1 else "XAUUSD")
        if broker_symbol not in symbols:
            raise RuntimeError("Selected broker symbol is not in the configured allowlist")
        info = adapter.symbol_info(broker_symbol)
        if info is None:
            raise RuntimeError(f"{broker_symbol} metadata unavailable")
        if not info.visible:
            if not adapter.symbol_select(broker_symbol):
                raise RuntimeError("Broker symbol selection failed")
            info = adapter.symbol_info(broker_symbol)
            if info is None:
                raise RuntimeError("Selected broker symbol metadata unavailable")
        report["symbol"] = asdict(info)
        tick = adapter.symbol_tick(broker_symbol)
        report["clock"] = {
            "utc_now": datetime.now(UTC).isoformat(),
            "server_tick_time": tick.time if tick else None,
            "configured_server_offset_seconds": args.server_offset_seconds,
        }
        report["baseline"] = {
            "orders": len(adapter.active_orders()),
            "positions": len(adapter.active_positions()),
        }
        write_report(report)
        print(json.dumps({"account": report["account"], "symbol": report["symbol"]}))
        if not args.execute and not args.recover_group:
            report["result"] = "probe_only"
            return
        if account.get("margin_mode") != 2 or not info.expiration_mode & 4:
            raise RuntimeError("Broker OCO requires a hedge account and specified symbol expiry")
        # Read the configured broker size without enabling the strategy's live loop.
        from backtesting_service.config import Settings as BacktestingSettings

        strategy_profile = args.strategy_profile or args.profile
        strategy_env = ROOT / f".env.{strategy_profile}"
        if not strategy_env.is_file():
            raise RuntimeError(
                "Strategy profile file is missing; pass --strategy-profile forex "
                "for the renamed HFM backtesting profile"
            )
        report["strategy_profile"] = strategy_profile
        strategy = BacktestingSettings(_env_file=strategy_env, market_execution_mode="off")
        volume = Decimal(str(strategy.execution_volume_lots))
        if volume > Decimal("0.01") or volume < Decimal(str(info.volume_min)):
            raise RuntimeError(
                "Configured test volume must fit broker limits and be at most 0.01 lot"
            )
        report["volume_lots"] = str(volume)
        state = ROOT / "data" / f"mt5-oco-verification.{args.profile}.sqlite3"
        repository = OcoRepository(state)
        repository.initialize()
        signals = SignalExecutionService(
            settings, adapter, SignalRepository(state.with_suffix(".signals.sqlite3"))
        )
        service = Mt5OcoService(signals, repository)
        await service.monitor_once(startup=True)
        capability = await service.capabilities(broker_symbol)
        report["capability"] = capability
        if not capability["ready"]:
            raise RuntimeError(f"Broker OCO not ready: {capability['reason']}")
        if args.recover_group:
            report["recovered_group"] = await service.get(UUID(args.recover_group))
            report["entry_requests"] = adapter.entry_requests
            report["result"] = "recovered"
            return
        report["checks"] = []
        for scenario in args.scenarios:
            tick = adapter.symbol_tick(broker_symbol)
            if tick is None:
                raise RuntimeError("Broker quote unavailable")
            point = Decimal(str(info.point))
            spread = Decimal(str(tick.ask)) - Decimal(str(tick.bid))
            buffer = max(point * (info.trade_stops_level + 5), Decimal("0.05"))
            stop = max(buffer * 2 + spread * 2, Decimal("1"))
            far = stop * 20
            upper = Decimal(str(tick.ask)) + (buffer if scenario == "long" else far)
            lower = Decimal(str(tick.bid)) - (buffer if scenario == "short" else far)
            now = datetime.now(UTC)
            group_id = uuid4()
            owned.append(group_id)
            request = OcoGroupRequest(
                group_id=group_id,
                profile=args.profile,
                occurred_at=now,
                decision_at=now,
                symbol=broker_symbol,
                volume=volume,
                upper_trigger=round(upper, info.digits),
                lower_trigger=round(lower, info.digits),
                stop_distance=round(stop, info.digits),
                target_distance=round(stop * 2, info.digits),
                expires_at=now + timedelta(seconds=args.expiry_seconds),
                source=strategy.execution_source,
            )
            group = await service.submit(request)
            check: dict[str, Any] = {
                "scenario": scenario,
                "group_id": str(group_id),
                "submission": group["state"],
                "submission_detail": group,
            }
            report["checks"].append(check)
            write_report(report)
            if group["state"] not in {"placed", "filled"}:
                check["reason"] = group.get("reason") or group.get("fault")
                if group["state"] == "rejected":
                    owned.remove(group_id)
                raise RuntimeError(f"{scenario} bracket was not placed: {check.get('reason')}")
            if scenario == "restart_disconnect":
                entries = adapter.entry_requests
                adapter.shutdown()
                await service.monitor_once()
                check["disconnect_detected"] = service.monitor_error is not None
                child = await asyncio.to_thread(
                    subprocess.run,
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--profile",
                        args.profile,
                        "--symbol",
                        broker_symbol,
                        "--server-offset-seconds",
                        str(args.server_offset_seconds),
                        "--recover-group",
                        str(group_id),
                        "--strategy-profile",
                        strategy_profile,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=25,
                )
                if child.returncode:
                    raise RuntimeError(f"Recovery subprocess failed: {child.stderr[-240:]}")
                recovered = json.loads(
                    (ROOT / "reports" / "mt5-oco-process-recovery.json").read_text(encoding="utf-8")
                )
                check["process_recovery"] = recovered
                if not adapter.initialize(settings):
                    raise RuntimeError("MT5 reconnection failed")
                service = Mt5OcoService(signals, repository)
                await service.monitor_once(startup=True)
                check["no_duplicate_entries"] = (
                    adapter.entry_requests == entries
                    and recovered["entry_requests"] == 0
                    and recovered["recovered_group"]["state"] == "placed"
                    and len(recovered["final_owned_orders"]) == 2
                )
                check["restored_state"] = (await service.get(group_id))["state"]
            deadline = time.monotonic() + (args.expiry_seconds + 10 if scenario == "expiry" else 30)
            while time.monotonic() < deadline:
                await service.monitor_once()
                group = await service.get(group_id)
                if scenario == "expiry" and group["state"] == "expired":
                    break
                if scenario != "expiry" and group.get("winner") is not None:
                    sibling = group["legs"]["short" if group["winner"] == "long" else "long"]
                    if sibling["state"] in {"cancelled", "expired", "rejected", "not_submitted"}:
                        break
                await asyncio.sleep(0.25)
            check["observed"] = group
            if scenario in {"long", "short"} and group.get("winner") == scenario:
                winner = group["legs"][scenario]
                sibling = group["legs"]["short" if scenario == "long" else "long"]
                protection = winner.get("applied_protection", {})
                check["fill_protection_confirmed"] = (
                    protection.get("anchor") == "actual_fill"
                    and protection.get("confirmed") is not False
                )
                check["sibling_terminal"] = (
                    sibling["state"] in {"cancelled", "expired", "rejected", "not_submitted"}
                    and sibling.get("resting") is False
                )
            check["passed"] = (
                group["state"] == "expired"
                if scenario == "expiry"
                else check.get("disconnect_detected") and check.get("no_duplicate_entries")
                if scenario == "restart_disconnect"
                else group.get("winner") == scenario
                and not group.get("fault")
                and check.get("fill_protection_confirmed")
                and check.get("sibling_terminal")
            )
            check["settled"] = await settle(service, group_id)
            owned.remove(group_id)
            write_report(report)
            print(
                json.dumps(
                    {"scenario": scenario, "passed": check["passed"], "winner": group.get("winner")}
                )
            )
        report["result"] = (
            "passed" if all(check["passed"] for check in report["checks"]) else "incomplete"
        )
    finally:
        if service is not None:
            if not adapter.connection_snapshot().connected:
                adapter.initialize(settings)
            for group_id in owned:
                try:
                    await settle(service, group_id)
                except Exception as exc:
                    report.setdefault("cleanup_errors", []).append(
                        {"group_id": str(group_id), "reason": type(exc).__name__}
                    )
            tags = {
                leg["broker_tag"]
                for group in service.repository.all()
                for leg in group["legs"].values()
            }
            position_ids = {
                pid
                for group in service.repository.all()
                for leg in group["legs"].values()
                for pid in leg.get("position_ids", [])
            }
            report["final_owned_orders"] = [
                row["ticket"] for row in adapter.active_orders() if row.get("comment") in tags
            ]
            report["final_owned_positions"] = [
                row["ticket"]
                for row in adapter.active_positions()
                if row.get("identifier", row.get("ticket")) in position_ids
            ]
        adapter.shutdown()
        write_report(report)


def main() -> None:
    global REPORT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="hfm")
    parser.add_argument("--strategy-profile", help="Backtesting profile name, e.g. forex")
    parser.add_argument(
        "--symbol", help="Exact configured broker symbol; defaults to a single-symbol allowlist"
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--recover-group", help=argparse.SUPPRESS)
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=["long", "short", "expiry", "restart_disconnect"],
        default=["long", "short", "expiry", "restart_disconnect"],
    )
    parser.add_argument("--report-name", default="mt5-oco-verification.json")
    parser.add_argument(
        "--expiry-seconds",
        type=int,
        default=120,
        help="Broker expiry duration; account servers may reject short deadlines",
    )
    parser.add_argument(
        "--server-offset-seconds",
        type=int,
        default=0,
        help="Verified broker server UTC offset; HFM currently uses 10800",
    )
    args = parser.parse_args()
    if Path(args.report_name).name != args.report_name:
        raise SystemExit("Report name must be a filename")
    REPORT = (
        ROOT
        / "reports"
        / ("mt5-oco-process-recovery.json" if args.recover_group else args.report_name)
    )
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "profile": args.profile,
        "execution_requested": args.execute,
        "result": "incomplete",
    }
    try:
        asyncio.run(verify(args, report))
    except ValidationError as exc:
        report["error"] = [
            {"location": error["loc"], "message": error["msg"]} for error in exc.errors()
        ]
        write_report(report)
        raise SystemExit("Invalid configuration; see the verification report") from None
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:240]}
        write_report(report)
        raise SystemExit(f"Verification incomplete: {report['error']['message']}") from None
    if args.execute and report["result"] != "passed":
        raise SystemExit("Some broker checks remain incomplete; see the verification report")


if __name__ == "__main__":
    main()
