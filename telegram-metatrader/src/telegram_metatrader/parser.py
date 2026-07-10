from __future__ import annotations

import re
import uuid
from decimal import Decimal, InvalidOperation

from .models import Direction, ParseFailure, ParsedSignal, Signal

ACTION_RE = re.compile(r"\b(buy|sell)\b", re.IGNORECASE)
KNOWN_SYMBOL_RE = re.compile(r"\b(gold|xau\s*/?\s*usd|xau)\b", re.IGNORECASE)
FOREX_SYMBOL_RE = re.compile(r"\b([A-Z]{3}\s*/?\s*[A-Z]{3})\b", re.IGNORECASE)
PRICE_RE = re.compile(r"(?<!\w)(\d{1,7}(?:\.\d+)?)(?!\w)")
ENTRY_RE = re.compile(r"\b(?:entry|enter|open|at|@)\s*[:=@-]?\s*(\d{1,7}(?:\.\d+)?)", re.IGNORECASE)
SL_RE = re.compile(r"\b(?:sl|s/l|stop\s*loss)\s*[:=@-]?\s*(\d{1,7}(?:\.\d+)?)", re.IGNORECASE)
TP_RE = re.compile(
    r"\b(?:tp|t/p|take\s*profit)\s*\d*\s*[:=@-]?\s*(\d{1,7}(?:\.\d+)?)",
    re.IGNORECASE,
)

SYMBOL_ALIASES = {
    "GOLD": "GOLD",
    "XAU": "XAU",
    "XAUUSD": "XAUUSD",
}


def parse_signal_text(text: str | None, *, source_chat: str, message_id: int, message_date: str) -> ParsedSignal:
    if text is None or text.strip() == "":
        return ParseFailure("empty_text")

    raw_text = text.strip()
    direction = _direction(raw_text)
    if isinstance(direction, ParseFailure):
        return direction

    symbol = _symbol(raw_text)
    if symbol is None:
        return ParseFailure("unknown_symbol")

    entry = _first_decimal(ENTRY_RE, raw_text)
    sl = _first_decimal(SL_RE, raw_text)
    all_tps = _all_decimals(TP_RE, raw_text)
    tp = all_tps[0] if all_tps else None

    if entry is None:
        entry = _fallback_entry(raw_text, {sl, *all_tps})

    return Signal(
        id=str(uuid.uuid4()),
        source_chat=source_chat,
        message_id=message_id,
        message_date=message_date,
        raw_text=raw_text,
        symbol=symbol,
        direction=direction,
        entry=entry,
        sl=sl,
        tp=tp,
        all_tps=all_tps,
    )


def _direction(text: str) -> Direction | ParseFailure:
    actions = [m.group(1).upper() for m in ACTION_RE.finditer(text)]
    unique = set(actions)
    if not actions:
        return ParseFailure("no_action")
    if len(unique) > 1:
        return ParseFailure("ambiguous_direction")
    return "BUY" if actions[0] == "BUY" else "SELL"


def _symbol(text: str) -> str | None:
    known = KNOWN_SYMBOL_RE.search(text)
    if known:
        key = _normalize_symbol(known.group(1))
        return SYMBOL_ALIASES.get(key, key)

    for match in FOREX_SYMBOL_RE.finditer(text):
        key = _normalize_symbol(match.group(1))
        if key in {"BUYSELL", "SELLBUY"}:
            continue
        return key
    return None


def _first_decimal(pattern: re.Pattern[str], text: str) -> Decimal | None:
    match = pattern.search(text)
    if not match:
        return None
    return _decimal(match.group(1))


def _all_decimals(pattern: re.Pattern[str], text: str) -> list[Decimal]:
    values: list[Decimal] = []
    for match in pattern.finditer(text):
        value = _decimal(match.group(1))
        if value is not None:
            values.append(value)
    return values


def _fallback_entry(text: str, excluded: set[Decimal | None]) -> Decimal | None:
    for match in PRICE_RE.finditer(text):
        value = _decimal(match.group(1))
        if value is not None and value not in excluded:
            return value
    return None


def _decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _normalize_symbol(raw: str) -> str:
    return raw.upper().replace("/", "").replace(" ", "").strip()

