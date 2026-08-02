"""Regenerate a symbols manifest so every instrument risks the same cash amount.

Two knobs in the manifest decide what a trade costs: the stop distance and the lot size.
They were previously set independently — stop distances tuned to clear the broker's
minimum, volumes left as hand-picked constants — so the cash at risk varied by more than
an order of magnitude across instruments.

This command ties them together:

    K        = reference stop distance / reference ATR   (gold's 25 pips, in ATR units)
    SL price = K x ATR(symbol)                           (the same room, volatility-scaled)
    lot      = RISK_USD / (SL price x contract size)     (the same cash risk everywhere)

ATR is the same Wilder ATR over the same target-timeframe bars the Supertrend entry uses,
so the stop is sized in the units the strategy actually trades in. The median across the
lookback window is used rather than the latest value: Crash/Boom series are dominated by
occasional spike bars, and the latest bar's ATR would pin sizing to whichever regime
happened to be running when the command was invoked.

Two hard constraints come from mt5-trader:

* Volumes must land on the broker's ``volume_min + n x volume_step`` grid, or the signal
  is rejected with 422 (``_validate_volume``). Lots therefore round **down** — realised
  risk lands at or under target, never over.
* A stop closer than ``trade_stops_level + spread`` is silently **widened** at execution
  (``_apply_distances``), which would push real risk above target with no error. The
  manifest's existing ``stop_loss_pips`` is used as that floor: those values were set to
  clear exactly that threshold.

When the broker's minimum lot risks more than the budget even so, the stop is shrunk to
fit the budget; if that would take it under the floor, the instrument is reported
UNSATISFIABLE and left untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

import httpx

from .broker_specs import BrokerSpec, lookup_spec
from .candles import Aggregator, Candle
from .config import Settings, load_settings, resolve_env_file, resolve_symbols_file
from .data_client import MarketDataClient
from .indicators import atr
from .instruments import InstrumentConfig, load_instruments_from_file

DEFAULT_RISK_USD = Decimal("25")
DEFAULT_REWARD_USD = Decimal("40")
DEFAULT_REFERENCE_SYMBOL = "XAUUSD"

STATUS_OK = "ok"
STATUS_CAPPED = "capped"
STATUS_VOLUME_CAPPED = "volume-capped"
STATUS_UNSATISFIABLE = "UNSATISFIABLE"
STATUS_NO_SPEC = "no-spec"
STATUS_NO_DATA = "no-data"

_APPLIED_STATUSES = frozenset({STATUS_OK, STATUS_CAPPED, STATUS_VOLUME_CAPPED})


@dataclass(frozen=True, slots=True)
class RiskTargets:
    risk_usd: Decimal = DEFAULT_RISK_USD
    reward_usd: Decimal = DEFAULT_REWARD_USD

    @property
    def reward_ratio(self) -> Decimal:
        return self.reward_usd / self.risk_usd


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """One instrument's calibrated sizing, plus what it actually costs."""

    quote: str
    mt5_symbol: str
    status: str
    atr_value: float | None = None
    stop_loss_pips: int | None = None
    take_profit_pips: int | None = None
    volume: Decimal | None = None
    risk_usd: Decimal | None = None
    reward_usd: Decimal | None = None
    note: str = ""

    @property
    def applied(self) -> bool:
        """Whether this result should replace the instrument's manifest values."""
        return self.status in _APPLIED_STATUSES


def quantize_volume(raw: Decimal, spec: BrokerSpec) -> Decimal:
    """Round ``raw`` down onto the broker's ``volume_min + n x volume_step`` grid.

    Rounding down keeps realised risk at or under the budget. The result is clamped into
    ``[volume_min, volume_max]``, and the upper clamp is itself snapped to the grid since
    ``volume_max`` is not guaranteed to sit on it.
    """
    if raw <= spec.volume_min:
        return spec.volume_min
    steps = ((raw - spec.volume_min) / spec.volume_step).to_integral_value(rounding=ROUND_FLOOR)
    volume = spec.volume_min + steps * spec.volume_step
    if volume > spec.volume_max:
        max_steps = ((spec.volume_max - spec.volume_min) / spec.volume_step).to_integral_value(
            rounding=ROUND_FLOOR
        )
        volume = spec.volume_min + max_steps * spec.volume_step
    return volume


def _pips_from_price(price: Decimal, pip_size: Decimal, *, rounding: str) -> int:
    return int((price / pip_size).to_integral_value(rounding=rounding))


def calibrate_instrument(
    instrument: InstrumentConfig,
    settings: Settings,
    spec: BrokerSpec | None,
    atr_value: float | None,
    *,
    sl_atr_multiple: Decimal,
    targets: RiskTargets,
) -> CalibrationResult:
    """Size one instrument's stop, target and lot against the cash risk budget."""
    quote = instrument.quote
    mt5_symbol = instrument.resolved_mt5_symbol()

    if spec is None:
        return CalibrationResult(
            quote=quote,
            mt5_symbol=mt5_symbol,
            status=STATUS_NO_SPEC,
            note="no entry in BROKER_SPECS; add the contract size and volume grid",
        )
    if atr_value is None or atr_value <= 0:
        return CalibrationResult(
            quote=quote,
            mt5_symbol=mt5_symbol,
            status=STATUS_NO_DATA,
            note="no usable ATR from the market-data feed",
        )

    pip_size = Decimal(str(instrument.resolved_pip_size(settings)))
    contract_size = spec.contract_size

    # The manifest's current stop is the empirical trade_stops_level + spread floor.
    floor_pips = int(Decimal(str(instrument.resolved_stop_loss_pips(settings))))

    target_price = Decimal(str(atr_value)) * sl_atr_multiple
    stop_pips = max(_pips_from_price(target_price, pip_size, rounding=ROUND_CEILING), floor_pips)

    status = STATUS_OK
    note = ""
    stop_price = stop_pips * pip_size
    raw_volume = targets.risk_usd / (stop_price * contract_size)

    if raw_volume < spec.volume_min:
        # Minimum lot overshoots the budget: the budget wins, so shrink the stop.
        shrunk_price = targets.risk_usd / (spec.volume_min * contract_size)
        shrunk_pips = _pips_from_price(shrunk_price, pip_size, rounding=ROUND_FLOOR)
        if shrunk_pips < floor_pips or shrunk_pips <= 0:
            actual = floor_pips * pip_size * spec.volume_min * contract_size
            return CalibrationResult(
                quote=quote,
                mt5_symbol=mt5_symbol,
                status=STATUS_UNSATISFIABLE,
                atr_value=atr_value,
                note=(
                    f"minimum lot {spec.volume_min} risks ${actual:.2f} at the broker's "
                    f"minimum stop ({floor_pips} pips); left unchanged"
                ),
            )
        stop_pips = shrunk_pips
        volume = spec.volume_min
        status = STATUS_CAPPED
        note = f"stop shrunk to hold ${targets.risk_usd} at the minimum lot"
    else:
        volume = quantize_volume(raw_volume, spec)
        if volume >= spec.volume_max:
            status = STATUS_VOLUME_CAPPED
            note = f"lot capped at the broker maximum {spec.volume_max}"

    take_profit_pips = int(
        (Decimal(stop_pips) * targets.reward_ratio).to_integral_value(rounding=ROUND_CEILING)
    )
    unit_value = volume * contract_size
    return CalibrationResult(
        quote=quote,
        mt5_symbol=mt5_symbol,
        status=status,
        atr_value=atr_value,
        stop_loss_pips=stop_pips,
        take_profit_pips=take_profit_pips,
        volume=volume,
        risk_usd=stop_pips * pip_size * unit_value,
        reward_usd=take_profit_pips * pip_size * unit_value,
        note=note,
    )


def derive_atr_multiple(
    reference: InstrumentConfig,
    settings: Settings,
    atr_value: float,
) -> Decimal:
    """Express the reference instrument's configured stop as a multiple of its ATR.

    Gold's 25 pips at ``pip_size`` 0.10 is a $2.50 stop; dividing by gold's ATR on the
    trading timeframe gives the "room" constant every other instrument is scaled to.
    """
    if atr_value <= 0:
        raise ValueError(f"reference {reference.quote} has no usable ATR")
    stop_price = Decimal(str(reference.resolved_stop_loss_pips(settings))) * Decimal(
        str(reference.resolved_pip_size(settings))
    )
    return stop_price / Decimal(str(atr_value))


def median_atr(minutes: list[Candle], settings: Settings) -> float | None:
    """Median Wilder ATR over the closed target-timeframe bars in the lookback window."""
    aggregator = Aggregator(settings.target_tf_minutes, settings.bucket_offset_minutes)
    series = aggregator.build(minutes)
    if len(series.closed) <= settings.supertrend_atr_len:
        return None
    values = [v for v in atr(series.closed, settings.supertrend_atr_len) if v is not None and v > 0]
    if not values:
        return None
    return statistics.median(values)


async def _fetch_atrs(
    instruments: list[InstrumentConfig],
    settings: Settings,
) -> dict[str, float | None]:
    async with httpx.AsyncClient() as http:
        client = MarketDataClient(settings, http)
        results = await asyncio.gather(
            *[client.fetch_minute_candles(inst.quote) for inst in instruments],
            return_exceptions=True,
        )
    atrs: dict[str, float | None] = {}
    for instrument, result in zip(instruments, results, strict=True):
        if isinstance(result, BaseException):
            print(f"warning: {instrument.quote}: fetch failed: {result}", file=sys.stderr)
            atrs[instrument.quote] = None
            continue
        atrs[instrument.quote] = median_atr(result, settings)
    return atrs


def _compact(pips: float) -> float | int:
    """Keep whole pip counts as ints so the manifest stays readable."""
    return int(pips) if float(pips).is_integer() else pips


def build_manifest(
    instruments: list[InstrumentConfig],
    settings: Settings,
    results: dict[str, CalibrationResult],
) -> list[dict]:
    """Re-emit the manifest with calibrated values, preserving every other field."""
    entries: list[dict] = []
    for instrument in instruments:
        result = results.get(instrument.quote)
        entry: dict = {"quote": instrument.quote, "mt5_symbol": instrument.resolved_mt5_symbol()}
        if instrument.pip_size is not None:
            entry["pip_size"] = instrument.pip_size
        if instrument.price_digits is not None:
            entry["price_digits"] = instrument.price_digits

        if result is not None and result.applied:
            assert result.volume is not None
            entry["volume"] = str(result.volume)
        elif instrument.volume is not None:
            entry["volume"] = str(instrument.volume)

        if instrument.deviation_points is not None:
            entry["deviation_points"] = instrument.deviation_points

        if result is not None and result.applied:
            entry["stop_loss_pips"] = result.stop_loss_pips
            entry["take_profit_pips"] = result.take_profit_pips
        else:
            if instrument.stop_loss_pips is not None:
                entry["stop_loss_pips"] = _compact(instrument.stop_loss_pips)
            if instrument.take_profit_pips is not None:
                entry["take_profit_pips"] = _compact(instrument.take_profit_pips)
        entries.append(entry)
    return entries


def format_table(results: list[CalibrationResult]) -> str:
    header = (
        f"{'symbol':<22} {'ATR':>12} {'SL pips':>10} {'TP pips':>10} "
        f"{'lot':>8} {'risk $':>9} {'reward $':>9}  status"
    )
    lines = [header, "-" * len(header)]
    for r in results:
        atr_text = f"{r.atr_value:.5g}" if r.atr_value is not None else "-"
        sl = str(r.stop_loss_pips) if r.stop_loss_pips is not None else "-"
        tp = str(r.take_profit_pips) if r.take_profit_pips is not None else "-"
        lot = str(r.volume) if r.volume is not None else "-"
        risk = f"{r.risk_usd:.2f}" if r.risk_usd is not None else "-"
        reward = f"{r.reward_usd:.2f}" if r.reward_usd is not None else "-"
        line = (
            f"{r.mt5_symbol:<22} {atr_text:>12} {sl:>10} {tp:>10} "
            f"{lot:>8} {risk:>9} {reward:>9}  {r.status}"
        )
        if r.note:
            line += f" ({r.note})"
        lines.append(line)
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calibrate a symbols manifest to a fixed cash risk per trade"
    )
    parser.add_argument("--profile", metavar="NAME", help="Load .env.NAME instead of .env")
    parser.add_argument(
        "--symbols-file",
        type=Path,
        help="Manifest to calibrate (defaults to the profile's SYMBOLS_FILE)",
    )
    parser.add_argument("--risk-usd", type=Decimal, default=DEFAULT_RISK_USD)
    parser.add_argument("--reward-usd", type=Decimal, default=DEFAULT_REWARD_USD)
    parser.add_argument(
        "--sl-atr-multiple",
        type=Decimal,
        help="Stop distance in ATR units; derived from the reference symbol when omitted",
    )
    parser.add_argument(
        "--reference",
        default=DEFAULT_REFERENCE_SYMBOL,
        help="Symbol whose configured stop defines the ATR multiple "
        f"(default {DEFAULT_REFERENCE_SYMBOL})",
    )
    parser.add_argument("--lookback", type=int, help="Override DATA_LOOKBACK for this run")
    parser.add_argument("--out", type=Path, help="Write the manifest here instead of in place")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the table without writing the manifest",
    )
    return parser.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    settings = load_settings(args.profile)
    if args.lookback is not None:
        settings = settings.model_copy(update={"data_lookback": args.lookback})

    env_file = resolve_env_file(args.profile)
    if args.symbols_file is not None:
        manifest_path = resolve_symbols_file(env_file, args.symbols_file)
        instruments = load_instruments_from_file(manifest_path)
    else:
        manifest_path = resolve_symbols_file(env_file, settings.symbols_file)
        instruments = list(settings.instruments)
    if manifest_path is None:
        print(
            "No manifest to calibrate: set SYMBOLS_FILE in the profile or pass --symbols-file",
            file=sys.stderr,
        )
        return 1

    targets = RiskTargets(risk_usd=args.risk_usd, reward_usd=args.reward_usd)
    atrs = await _fetch_atrs(instruments, settings)

    sl_atr_multiple = args.sl_atr_multiple
    if sl_atr_multiple is None:
        reference = next(
            (i for i in instruments if i.resolved_mt5_symbol() == args.reference), None
        )
        reference_atr = atrs.get(reference.quote) if reference is not None else None
        if reference is None or reference_atr is None:
            print(
                f"Cannot derive the ATR multiple: {args.reference} is not in this manifest "
                f"with usable data. Run the profile that holds it first, then pass the "
                f"printed value as --sl-atr-multiple.",
                file=sys.stderr,
            )
            return 1
        sl_atr_multiple = derive_atr_multiple(reference, settings, reference_atr)
        print(
            f"Reference {args.reference}: ATR {reference_atr:.5g}, "
            f"stop {reference.resolved_stop_loss_pips(settings):g} pips "
            f"-> --sl-atr-multiple {sl_atr_multiple:.4f}"
        )

    results = [
        calibrate_instrument(
            instrument,
            settings,
            lookup_spec(instrument.resolved_mt5_symbol()),
            atrs.get(instrument.quote),
            sl_atr_multiple=sl_atr_multiple,
            targets=targets,
        )
        for instrument in instruments
    ]

    print(f"\nTargets: ${targets.risk_usd} risk / ${targets.reward_usd} reward")
    print(format_table(results))

    by_quote = {r.quote: r for r in results}
    manifest = build_manifest(instruments, settings, by_quote)
    skipped = [r for r in results if not r.applied]
    if skipped:
        print(
            f"\n{len(skipped)} instrument(s) left unchanged: "
            + ", ".join(f"{r.mt5_symbol} ({r.status})" for r in skipped)
        )

    if args.dry_run:
        print("\n--dry-run: manifest not written")
        return 0

    out_path = args.out if args.out is not None else manifest_path
    out_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


def run(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        sys.exit(asyncio.run(amain(args)))
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run()
