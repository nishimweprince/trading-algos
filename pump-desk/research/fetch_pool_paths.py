#!/usr/bin/env python3
"""Fetch compact post-migration PumpSwap reserve paths on the key-holding VM.

The Helius URL is read only from HELIUS_HTTP_URL. Raw RPC responses and the URL
are never written. The output contains pool metadata and the base/quote reserve
state after each transaction in the requested horizon.
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
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL_MINT = "So11111111111111111111111111111111111111112"
POOL_DISCRIMINATOR = bytes.fromhex("f19a6d0411b16dbc")
PDA_MARKER = b"ProgramDerivedAddress"


def b58decode(value: str) -> bytes:
    number = 0
    for char in value:
        number = number * 58 + ALPHABET.index(char)
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + body


def b58encode(value: bytes) -> str:
    number = int.from_bytes(value, "big")
    out = ""
    while number:
        number, remainder = divmod(number, 58)
        out = ALPHABET[remainder] + out
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + out


def is_ed25519_point(value: bytes) -> bool:
    """Return whether 32 compressed bytes decode to an Ed25519 point."""
    if len(value) != 32:
        return False
    prime = 2**255 - 19
    y = int.from_bytes(value, "little") & ((1 << 255) - 1)
    if y >= prime:
        return False
    d = (-121665 * pow(121666, prime - 2, prime)) % prime
    y2 = y * y % prime
    x2 = (y2 - 1) * pow(d * y2 + 1, prime - 2, prime) % prime
    return x2 == 0 or pow(x2, (prime - 1) // 2, prime) == 1


def find_program_address(seeds: list[bytes], program: str) -> bytes:
    program_bytes = b58decode(program)
    for bump in range(255, -1, -1):
        material = b"".join([*seeds, bytes([bump]), program_bytes, PDA_MARKER])
        digest = hashlib.sha256(material).digest()
        if not is_ed25519_point(digest):
            return digest
    raise ValueError("no viable program-derived address")


def canonical_pool(mint: str) -> str:
    mint_bytes = b58decode(mint)
    authority = find_program_address([b"pool-authority", mint_bytes], PUMP_PROGRAM)
    pool = find_program_address(
        [b"pool", b"\0\0", authority, mint_bytes, b58decode(WSOL_MINT)],
        PUMP_AMM_PROGRAM,
    )
    return b58encode(pool)


def decode_pool(data: bytes, owner: str, address: str) -> dict[str, Any] | None:
    if owner != PUMP_AMM_PROGRAM or len(data) < 245 or data[:8] != POOL_DISCRIMINATOR:
        return None
    virtual = int.from_bytes(data[245:261], "little", signed=True) if len(data) >= 261 else 0
    return {
        "pool": address,
        "base_mint": b58encode(data[43:75]),
        "quote_mint": b58encode(data[75:107]),
        "coin_creator": b58encode(data[211:243]),
        "mayhem": data[243] == 1,
        "cashback": data[244] == 1,
        "virtual_quote": max(0, virtual),
        "creator_fee_bps": int.from_bytes(data[261:269], "little") if len(data) >= 269 else 0,
    }


@dataclass(frozen=True)
class Migration:
    mint: str
    timestamp_ms: int
    slot: int
    signature: str
    caller: str


class Rpc:
    def __init__(self, url: str, rps: float, timeout: float = 45.0) -> None:
        self.url = url
        self.timeout = timeout
        self.interval = 1.0 / rps if rps > 0 else 0.0
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.ids = 0

    def call(self, method: str, params: list[Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(7):
            with self.lock:
                wait = self.next_at - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self.next_at = time.monotonic() + self.interval
                self.ids += 1
                request_id = self.ids
            body = json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            ).encode()
            request = urllib.request.Request(
                self.url, data=body, headers={"content-type": "application/json"}
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.load(response)
                if payload.get("error"):
                    raise RuntimeError(f"{method}: {payload['error']}")
                return payload.get("result")
            except (OSError, TimeoutError, urllib.error.HTTPError, RuntimeError) as error:
                last_error = error
                retryable = not isinstance(error, urllib.error.HTTPError) or error.code in {
                    408,
                    429,
                    500,
                    502,
                    503,
                    504,
                }
                if not retryable or attempt == 6:
                    break
                time.sleep(min(30.0, 0.5 * 2**attempt))
        raise RuntimeError(f"{method} failed after retries: {last_error}")


def load_migrations(db_path: Path) -> list[Migration]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = connection.execute(
        """
        SELECT m.mint, m.ts, m.slot, m.sig, m.caller
          FROM migrations m
          JOIN (
                SELECT mint, MIN(ts) AS first_ts
                  FROM migrations
                 WHERE landed = 1
                 GROUP BY mint
          ) first ON first.mint = m.mint AND first.first_ts = m.ts
         WHERE m.landed = 1
         GROUP BY m.mint
         ORDER BY m.ts, m.mint
        """
    ).fetchall()
    connection.close()
    return [Migration(str(a), int(b), int(c), str(d), str(e)) for a, b, c, d, e in rows]


def signer_of(transaction: dict[str, Any]) -> str | None:
    keys = transaction.get("transaction", {}).get("message", {}).get("accountKeys", [])
    for key in keys:
        if isinstance(key, dict) and key.get("signer"):
            return str(key.get("pubkey"))
    return None


def compact_points(
    transactions: list[dict[str, Any]], pool: str, base_mint: str, quote_mint: str
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for transaction in transactions:
        meta = transaction.get("meta") or {}
        if meta.get("err") is not None:
            continue
        balances = meta.get("postTokenBalances") or []
        owned = [balance for balance in balances if balance.get("owner") == pool]
        base = next(
            (
                balance.get("uiTokenAmount", {}).get("amount")
                for balance in owned
                if balance.get("mint") == base_mint
            ),
            None,
        )
        quote = next(
            (
                balance.get("uiTokenAmount", {}).get("amount")
                for balance in owned
                if balance.get("mint") == quote_mint
            ),
            None,
        )
        signatures = transaction.get("transaction", {}).get("signatures") or []
        if base is None or quote is None or not signatures:
            continue
        points.append(
            {
                "signature": str(signatures[0]),
                "slot": int(transaction.get("slot") or 0),
                "transaction_index": int(transaction.get("transactionIndex") or 0),
                "block_time": int(transaction.get("blockTime") or 0),
                "signer": signer_of(transaction),
                "base": int(base),
                "quote": int(quote),
            }
        )
    unique = {point["signature"]: point for point in points}
    return sorted(unique.values(), key=lambda point: (point["slot"], point["transaction_index"]))


def fetch_one(
    rpc: Rpc, migration: Migration, horizon_seconds: int, max_pages: int
) -> dict[str, Any]:
    pool = canonical_pool(migration.mint)
    account = rpc.call("getAccountInfo", [pool, {"encoding": "base64", "commitment": "confirmed"}])
    value = (account or {}).get("value")
    common = {
        "mint": migration.mint,
        "migration_ts": migration.timestamp_ms,
        "migration_slot": migration.slot,
        "migration_signature": migration.signature,
        "caller": migration.caller,
        "pool": pool,
    }
    if not value:
        return {**common, "status": "no_wsol_pool", "points": []}
    encoded = value.get("data")
    if not isinstance(encoded, list) or not encoded:
        return {**common, "status": "bad_pool_account", "points": []}
    metadata = decode_pool(base64.b64decode(encoded[0]), str(value.get("owner")), pool)
    if not metadata:
        return {**common, "status": "bad_pool_account", "points": []}
    if metadata["base_mint"] != migration.mint or metadata["quote_mint"] != WSOL_MINT:
        return {**common, **metadata, "status": "non_wsol", "points": []}

    start = migration.timestamp_ms // 1000 - 2
    end = migration.timestamp_ms // 1000 + horizon_seconds + 2
    transactions: list[dict[str, Any]] = []
    token: str | None = None
    pages = 0
    while pages < max_pages:
        options: dict[str, Any] = {
            "transactionDetails": "full",
            "encoding": "jsonParsed",
            "sortOrder": "asc",
            "limit": 100,
            "filters": {
                "blockTime": {"gte": start, "lte": end},
                "status": "succeeded",
            },
        }
        if token:
            options["paginationToken"] = token
        result = rpc.call("getTransactionsForAddress", [pool, options]) or {}
        transactions.extend(result.get("data") or [])
        token = result.get("paginationToken")
        pages += 1
        if not token:
            break
    points = compact_points(transactions, pool, metadata["base_mint"], metadata["quote_mint"])
    return {
        **common,
        **metadata,
        "status": "ok",
        "rpc_pages": pages,
        "truncated": bool(token),
        "points": points,
    }


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
            if row.get("status") in {"ok", "non_wsol", "no_wsol_pool", "bad_pool_account"}:
                records[str(row["mint"])] = row
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("pump-desk/data/pump-desk.db"))
    parser.add_argument(
        "--output", type=Path, default=Path("pump-desk/research/data/pool-paths.json.gz")
    )
    parser.add_argument("--horizon-seconds", type=int, default=60)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--rps", type=float, default=12.0)
    parser.add_argument("--max-pages", type=int, default=50)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    url = os.environ.get("HELIUS_HTTP_URL", "").strip()
    if not url:
        raise SystemExit("HELIUS_HTTP_URL is required (run this only on the guarded VM)")
    migrations = load_migrations(args.db)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    partial = args.output.with_suffix(args.output.suffix + ".partial.jsonl")
    records = read_partial(partial)
    pending = [migration for migration in migrations if migration.mint not in records]
    rpc = Rpc(url, args.rps)
    print(f"migrations={len(migrations)} cached={len(records)} pending={len(pending)}")
    write_lock = threading.Lock()
    completed = 0
    with partial.open("a", encoding="utf-8") as handle:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    fetch_one,
                    rpc,
                    migration,
                    args.horizon_seconds,
                    args.max_pages,
                ): migration
                for migration in pending
            }
            for future in concurrent.futures.as_completed(futures):
                migration = futures[future]
                try:
                    row = future.result()
                except Exception as error:  # a resumable fetch should finish the rest
                    row = {
                        "mint": migration.mint,
                        "status": "error",
                        "error": str(error)[:300],
                    }
                if row.get("status") != "error":
                    records[migration.mint] = row
                with write_lock:
                    handle.write(json.dumps(row, separators=(",", ":")) + "\n")
                    handle.flush()
                completed += 1
                if completed % 10 == 0 or completed == len(pending):
                    print(f"completed={completed}/{len(pending)} status={row.get('status')}")
    ordered = [records[migration.mint] for migration in migrations if migration.mint in records]
    temp = args.output.with_suffix(args.output.suffix + ".tmp")
    with gzip.open(temp, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(
            {
                "schema": 1,
                "horizon_seconds": args.horizon_seconds,
                "generated_at": int(time.time()),
                "records": ordered,
            },
            handle,
            separators=(",", ":"),
        )
    temp.replace(args.output)
    counts: dict[str, int] = {}
    for row in ordered:
        status = str(row.get("status"))
        counts[status] = counts.get(status, 0) + 1
    print(f"wrote={args.output} records={len(ordered)} status={counts}")


if __name__ == "__main__":
    main()
