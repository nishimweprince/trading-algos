"""Format a Signal into notification message bodies or template parameters."""
from typing import List

from app.core.types import Direction, Signal

SMS_MAX_CHARS = 150


def format_signal_text(signal: Signal) -> str:
    """Human-readable free-form message body."""
    arrow = '🟢 BUY' if signal.direction == Direction.BUY else '🔴 SELL'
    confidence = ', '.join(signal.confidence) if signal.confidence else '—'
    rr = f"{signal.rr:.2f}" if signal.rr else '—'
    swept = _format_swept(signal)
    fu_ohlc = _format_fu_ohlc(signal)
    details = [
        f"{arrow} {signal.symbol} ({signal.timeframe})",
        f"Entry: {_compact_number(signal.entry_price)}",
        f"SL: {_compact_number(signal.sl)}",
        f"TP: {_compact_number(signal.tp)}",
        f"R:R: {rr}",
        f"Bias: {signal.structure_bias.value}",
    ]
    if swept:
        details.append(swept)
    if fu_ohlc:
        details.append(fu_ohlc)
    details.extend([
        f"Confluence: {confidence}",
        f"FU @ {signal.fu_candle_time.isoformat()}",
    ])

    return '\n'.join(details)


def format_signal_sms_text(signal: Signal) -> str:
    """Concise SMS body, capped to one 150-character billing segment."""
    rr = f"{signal.rr:.2f}" if signal.rr else 'n/a'
    fu_time = signal.fu_candle_time.strftime('%m-%d %H:%MZ')
    swept = (
        f" SW:{_compact_number(signal.swept_level)}"
        if signal.swept_level is not None else ''
    )
    body = (
        f"FU {signal.direction.value} {signal.symbol} {signal.timeframe} "
        f"E:{_compact_number(signal.entry_price)} "
        f"SL:{_compact_number(signal.sl)} "
        f"TP:{_compact_number(signal.tp)} "
        f"RR:{rr} BI:{signal.structure_bias.value}{swept} @{fu_time}"
    )
    return body[:SMS_MAX_CHARS]


def _compact_number(value: float) -> str:
    return f"{value:.8g}"


def _format_swept(signal: Signal) -> str:
    if signal.swept_level is None:
        return ''
    label = 'previous low' if signal.direction == Direction.BUY else 'previous high'
    return f"Swept {label}: {_compact_number(signal.swept_level)}"


def _format_fu_ohlc(signal: Signal) -> str:
    values = (signal.fu_open, signal.fu_high, signal.fu_low, signal.fu_close)
    if any(value is None for value in values):
        return ''
    o, h, l, c = values
    return (
        f"FU OHLC: O:{_compact_number(o)} H:{_compact_number(h)} "
        f"L:{_compact_number(l)} C:{_compact_number(c)}"
    )


def format_signal_template_params(signal: Signal) -> List[str]:
    """Parameters substituted into a WhatsApp template's body placeholders.

    Order is the contract you must keep in sync with the approved template:
      {{1}} = direction
      {{2}} = symbol
      {{3}} = timeframe
      {{4}} = entry
      {{5}} = SL
      {{6}} = TP
      {{7}} = R:R
      {{8}} = bias
    """
    rr = f"{signal.rr:.2f}" if signal.rr else 'n/a'
    return [
        signal.direction.value,
        signal.symbol,
        signal.timeframe,
        str(signal.entry_price),
        str(signal.sl),
        str(signal.tp),
        rr,
        signal.structure_bias.value,
    ]
