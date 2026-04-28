"""Format a Signal into a WhatsApp message body or template parameters."""
from typing import List

from app.core.types import Direction, Signal


def format_signal_text(signal: Signal) -> str:
    """Human-readable free-form message body."""
    arrow = '🟢 BUY' if signal.direction == Direction.BUY else '🔴 SELL'
    confidence = ', '.join(signal.confidence) if signal.confidence else '—'
    rr = f"{signal.rr:.2f}" if signal.rr else '—'

    return (
        f"{arrow} {signal.symbol} ({signal.timeframe})\n"
        f"Entry: {signal.entry_price}\n"
        f"SL: {signal.sl}\n"
        f"TP: {signal.tp}\n"
        f"R:R: {rr}\n"
        f"Bias: {signal.structure_bias.value}\n"
        f"Confluence: {confidence}\n"
        f"FU @ {signal.fu_candle_time.isoformat()}"
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
