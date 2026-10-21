#!/usr/bin/env python3
"""Fetch bonding-curve paths (and the post-migration pool path) on the key-holding VM.

For every curve pump-desk admitted while still incomplete (curve_admit with a
positive completion cost), this records the curve's reserves after each
transaction from admission until completion (or a fixed horizon when the curve
never completed), and, when the mint migrated, the canonical PumpSwap pool path
for the first seconds after the migrate. Curves that never completed are kept:
the replay needs them to price the stall exits.

The Helius URL is read only from HELIUS_HTTP_URL. Raw RPC responses and the URL
are never written.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import gzip
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from fetch_pool_paths import (
    PUMP_PROGRAM,
    Migration,
    Rpc,
    b58decode,
    b58encode,
    fetch_one,
    find_program_address,
    signer_of,
)

TRADE_EVENT = hashlib.sha256(b"event:TradeEvent").digest()[:8]
EVENT_CPI_TAG = bytes.fromhex("e445a52e51cb9a1d")


def load_admits(db_path: Path) -> list[dict[str, Any]]:
    """First positive-cost admission per curve, plus the landed migrate of its mint if any."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    admits = connection.execute(
        """
        SELECT a, MIN(ts), slot, d0 FROM events
         WHERE type = 'curve_admit' AND d0 > 0
         GROUP BY a ORDER BY MIN(ts)
        """
    ).fetchall()
    migrations: dict[str, tuple[str, int, int, str, str]] = {}
    for mint, ts, slot, sig, caller in connection.execute(
        """
        SELECT mint, MIN(ts), slot, sig, caller FROM migrations
         WHERE landed = 1 AND mint IS NOT NULL AND mint != ''
         GROUP BY mint
        """
    ):
        curve = b58encode(find_program_address([b"bonding-curve", b58decode(mint)], PUMP_PROGRAM))
        migrations[curve] = (str(mint), int(ts), int(slot), str(sig), str(caller))
    connection.close()
    out = []
    for curve, ts, slot, cost in admits:
        row: dict[str, Any] = {
            "curve": str(curve),
            "admit_ts": int(ts),
            "admit_slot": int(slot),
            "admit_cost_sol": float(cost),
        }
        migration = migrations.get(str(curve))
        if migration:
            mint, mts, mslot, msig, caller = migration
            row.update(mint=mint, migration_ts=mts, migration_slot=mslot,
                       migration_signature=msig, caller=caller)
        out.append(row)
    return out


def u64(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 8], "little")


def decode_trade_event(data: bytes) -> dict[str, Any] | None:
    """pump.fun TradeEvent (after its 8-byte discriminator)."""
    if len(data) < 129 or data[:8] != TRADE_EVENT:
        return None
    event = {
        "mint": b58encode(data[8:40]),
        "sol_amount": u64(data, 40),
        "token_amount": u64(data, 48),
        "is_buy": data[56] == 1,
        "user": b58encode(data[57:89]),
        "vsol": u64(data, 97),
        "vtok": u64(data, 105),
        "rsol": u64(data, 113),
        "rtok": u64(data, 121),
    }
    if len(data) >= 225:
        event["fee_bps"] = u64(data, 161)
        event["creator_fee_bps"] = u64(data, 209)
    return event


def trade_events(transaction: dict[str, Any]) -> list[dict[str, Any]]:
    """TradeEvents from event-CPI inner instructions, else from 'Program data:' logs."""
    meta = transaction.get("meta") or {}
    events: list[dict[str, Any]] = []
    for group in meta.get("innerInstructions") or []:
        for ix in group.get("instructions") or []:
            raw = ix.get("data")
            if ix.get("programId") != PUMP_PROGRAM or not isinstance(raw, str):
                continue
            try:
                data = b58decode(raw)
            except ValueError:
                continue
            if data[:8] == EVENT_CPI_TAG:
                event = decode_trade_event(data[8:])
                if event:
                    events.append(event)
    if events:
        return events
    for line in meta.get("logMessages") or []:
        if line.startswith("Program data: "):
            try:
                event = decode_trade_event(base64.b64decode(line[14:]))
            except ValueError:
                continue
            if event:
                events.append(event)
    return events


def curve_points(transactions: list[dict[str, Any]], mint_hint: str | None) -> tuple[str | None, list[dict[str, Any]]]:
    points: list[dict[str, Any]] = []
    mint = mint_hint
    for transaction in transactions:
        meta = transaction.get("meta") or {}
        if meta.get("err") is not None:
            continue
        signatures = transaction.get("transaction", {}).get("signatures") or []
        if not signatures:
            continue
        for n, event in enumerate(trade_events(transaction)):
            if mint is None:
                mint = event["mint"]
            if event["mint"] != mint:
                continue
            points.append(
                {
                    "signature": str(signatures[0]),
                    "slot": int(transaction.get("slot") or 0),
                    "transaction_index": int(transaction.get("transactionIndex") or 0),
                    "event_index": n,
                    "block_time": int(transaction.get("blockTime") or 0),
                    "signer": signer_of(transaction),
                    "is_buy": event["is_buy"],
                    "sol_amount": event["sol_amount"],
                    "vsol": event["vsol"],
                    "vtok": event["vtok"],
                    "rsol": event["rsol"],
                    "rtok": event["rtok"],
                    "fee_bps": event.get("fee_bps"),
                    "creator_fee_bps": event.get("creator_fee_bps"),
                }
            )
    points.sort(key=lambda p: (p["slot"], p["transaction_index"], p["event_index"]))
    return mint, points


def fetch_curve(rpc: Rpc, admit: dict[str, Any], lookback_seconds: int, stall_horizon_seconds: int,
                pool_horizon_seconds: int, max_pages: int) -> dict[str, Any]:
    start = admit["admit_ts"] // 1000 - lookback_seconds
    if "migration_ts" in admit:
        end = admit["migration_ts"] // 1000 + 2
    else:
        end = admit["admit_ts"] // 1000 + stall_horizon_seconds
    transactions: list[dict[str, Any]] = []
    token: str | None = None
    pages = 0
    while pages < max_pages:
        options: dict[str, Any] = {
            "transactionDetails": "full",
            "encoding": "jsonParsed",
            "sortOrder": "asc",
            "limit": 100,
            "maxSupportedTransactionVersion": 1,
            "filters": {"blockTime": {"gte": start, "lte": end}, "status": "succeeded"},
        }
        if token:
            options["paginationToken"] = token
        result = rpc.call("getTransactionsForAddress", [admit["curve"], options]) or {}
        transactions.extend(result.get("data") or [])
        token = result.get("paginationToken")
        pages += 1
        if not token:
            break
    mint, points = curve_points(transactions, admit.get("mint"))
    record: dict[str, Any] = {
        **admit,
        "mint": mint,
        "status": "ok" if points else "no_trades",
        "curve_pages": pages,
        "curve_truncated": bool(token),
        "curve_points": points,
    }
    if "migration_ts" in admit and mint:
        migration = Migration(mint, admit["migration_ts"], admit["migration_slot"],
                              admit["migration_signature"], admit["caller"])
        pool = fetch_one(rpc, migration, pool_horizon_seconds, max_pages)
        record["pool"] = {key: value for key, value in pool.items()
                          if key not in {"mint", "migration_ts", "migration_slot",
                                         "migration_signature", "caller"}}
    return record


def read_partial(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return records
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") in {"ok", "no_trades"}:
                records[str(row["curve"])] = row
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("pump-desk/data/pump-desk.db"))
    parser.add_argument("--output", type=Path, default=Path("pump-desk/research/data/curve-paths.json.gz"))
    parser.add_argument("--lookback-seconds", type=int, default=5)
    parser.add_argument("--stall-horizon-seconds", type=int, default=900)
    parser.add_argument("--pool-horizon-seconds", type=int, default=60)
    parser.add_argument("--limit", type=int, default=0, help="first N curves only (smoke test)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--rps", type=float, default=8.0)
    parser.add_argument("--max-pages", type=int, default=30)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    url = os.environ.get("HELIUS_HTTP_URL", "").strip()
    if not url:
        raise SystemExit("HELIUS_HTTP_URL is required (run this only on the guarded VM)")
    admits = load_admits(args.db)
    if args.limit:
        admits = admits[: args.limit]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    partial = args.output.with_suffix(args.output.suffix + ".partial.jsonl")
    records = read_partial(partial)
    pending = [admit for admit in admits if admit["curve"] not in records]
    rpc = Rpc(url, args.rps)
    print(f"curves={len(admits)} migrated={sum('migration_ts' in a for a in admits)} "
          f"cached={len(records)} pending={len(pending)}")
    write_lock = threading.Lock()
    completed = 0
    with partial.open("a", encoding="utf-8") as handle:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(fetch_curve, rpc, admit, args.lookback_seconds,
                                args.stall_horizon_seconds, args.pool_horizon_seconds,
                                args.max_pages): admit
                for admit in pending
            }
            for future in concurrent.futures.as_completed(futures):
                admit = futures[future]
                try:
                    row = future.result()
                except Exception as error:  # a resumable fetch should finish the rest
                    row = {"curve": admit["curve"], "status": "error", "error": str(error)[:300]}
                if row.get("status") != "error":
                    records[admit["curve"]] = row
                with write_lock:
                    handle.write(json.dumps(row, separators=(",", ":")) + "\n")
                    handle.flush()
                completed += 1
                if completed % 10 == 0 or completed == len(pending):
                    print(f"completed={completed}/{len(pending)} status={row.get('status')}")
    ordered = [records[a["curve"]] for a in admits if a["curve"] in records]
    temp = args.output.with_suffix(args.output.suffix + ".tmp")
    with gzip.open(temp, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump({"schema": 1, "generated_at": int(time.time()), "records": ordered},
                  handle, separators=(",", ":"))
    temp.replace(args.output)
    counts: dict[str, int] = {}
    for row in ordered:
        counts[str(row.get("status"))] = counts.get(str(row.get("status")), 0) + 1
    print(f"wrote={args.output} records={len(ordered)} status={counts}")


if __name__ == "__main__":
    main()
