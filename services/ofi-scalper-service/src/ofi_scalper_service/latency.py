"""Stage 0 measurement: ``ofi-latency --profile dev [--seconds 120]``.

Writes ``research/latency.json`` with, tagged by ``OFI_HOST_TAG``:

- **clock offset** to Binance, from ``/fapi/v1/time`` (best of N by RTT);
- **feed latency** per stream: local receive time minus Binance event time,
  offset-corrected, P50/P90/P99;
- **REST RTT**: unsigned (``/fapi/v1/time``) and, with the read-only key,
  signed (``/fapi/v1/accountConfig``);
- **fees**: commission rates and fee tier, when a key is configured.

Order-entry RTT is **not** measured: that needs a trading key, which does not
exist until Stage 4. The signed GET round trip is the closest proxy.

Numbers from a laptop (``host=mac``) are for plumbing only; the §1.7 gates
need numbers from the Tokyo host.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ta_core import base_parser, load_or_exit
from ta_plugin_api import MARKET_DATA_GROUP, load_providers
from ta_plugin_binance_futures.streams import AggTrade, BookSnapshot, BookTick, DepthUpdate

from .config import PROVIDER, load_settings

__all__ = ["percentiles", "run"]


def percentiles(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)

    def pick(q: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 3)

    return {
        "p50": pick(0.5),
        "p90": pick(0.9),
        "p99": pick(0.99),
        "max": round(ordered[-1], 3),
        "mean": round(statistics.fmean(ordered), 3),
        "n": len(ordered),
    }


async def measure(settings: Any, seconds: float, samples: int) -> dict[str, Any]:
    factory = load_providers(MARKET_DATA_GROUP, [PROVIDER])[PROVIDER]
    rest = factory.rest(settings)
    account = factory.account(settings, rest=rest)
    streams = factory.streams(settings, rest=rest)
    result: dict[str, Any] = {
        "host": settings.host_tag,
        "measured_at": datetime.now(UTC).isoformat(),
        "symbols": list(settings.binance_futures_symbols),
        "valid_for_gates": settings.host_tag != "mac",
    }
    try:
        # Clock offset: server time minus local midpoint, from the fastest sample.
        best: tuple[float, float] | None = None
        unsigned: list[float] = []
        for _ in range(samples):
            before = time.time_ns()
            server_ms = await account.server_time_ms()
            after = time.time_ns()
            rtt_ms = (after - before) / 1e6
            unsigned.append(rtt_ms)
            offset_ms = server_ms - (before + after) / 2e6
            if best is None or rtt_ms < best[0]:
                best = (rtt_ms, offset_ms)
            await asyncio.sleep(0.2)
        assert best is not None
        offset = best[1]
        result["clock_offset_ms"] = {"value": round(offset, 3), "from_rtt_ms": round(best[0], 3)}
        result["rest_rtt_ms"] = {"unsigned": percentiles(unsigned)}

        if account.available:
            signed = [await account.timed(signed=True) for _ in range(samples)]
            result["rest_rtt_ms"]["signed"] = percentiles(signed)
            config = await account.account_config()
            rates = {
                symbol: await account.commission_rate(symbol)
                for symbol in settings.binance_futures_symbols
            }
            result["fees"] = {
                "fee_tier": config.get("feeTier"),
                "bnb_discount_setting": settings.bnb_fee_discount,
                "symbols": {
                    s: {"maker_bp": r.maker_bp, "taker_bp": r.taker_bp} for s, r in rates.items()
                },
            }
        else:
            result["rest_rtt_ms"]["signed"] = None
            result["fees"] = None
        result["order_rtt_ms"] = None  # needs a trading key (Stage 4)

        feed: dict[str, list[float]] = {"depth": [], "bookTicker": [], "aggTrade": []}
        await streams.start()
        deadline = time.monotonic() + seconds

        async def consume() -> None:
            async for event in streams.events():
                if isinstance(event, DepthUpdate | BookSnapshot):
                    kind = "depth"
                elif isinstance(event, BookTick):
                    kind = "bookTicker"
                elif isinstance(event, AggTrade):
                    kind = "aggTrade"
                else:
                    continue
                feed[kind].append(event.recv_ns / 1e6 + offset - event.event_ms)
                if time.monotonic() >= deadline:
                    return

        try:
            await asyncio.wait_for(consume(), seconds + 30)
        except TimeoutError:
            pass
        result["feed_latency_ms"] = {kind: percentiles(values) for kind, values in feed.items()}
        result["feed_seconds"] = seconds
        result["book_mode"] = streams.mode
        result["subscriptions"] = {
            route: streams.stream_names(route) for route in ("public", "market")
        }
        _, details = streams.readiness()
        result["stream_health"] = {
            "reconnects": details["reconnects"],
            "books": details["books"],
        }
    finally:
        await streams.close()
        await rest.aclose()
    return result


def run(argv: list[str] | None = None) -> None:
    parser = base_parser("Measure feed latency, REST RTT, clock offset and fees")
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--out", type=Path, default=Path("research/latency.json"))
    args = parser.parse_args(argv)
    settings = load_or_exit(load_settings, args.profile)
    result = asyncio.run(measure(settings, args.seconds, args.samples))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    run()
