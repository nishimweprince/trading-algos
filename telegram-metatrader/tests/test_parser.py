from __future__ import annotations

from decimal import Decimal

from telegram_metatrader.models import ParseFailure, Signal
from telegram_metatrader.parser import parse_signal_text


def parse(text: str):
    return parse_signal_text(text, source_chat="-1001", message_id=7, message_date="2026-07-10T00:00:00Z")


def test_parse_gold_buy_with_sl_and_multiple_tps():
    parsed = parse("BUY GOLD\nEntry 2320\nSL 2310\nTP1 2330\nTP2 2340")

    assert isinstance(parsed, Signal)
    assert parsed.direction == "BUY"
    assert parsed.symbol == "GOLD"
    assert parsed.entry == Decimal("2320")
    assert parsed.sl == Decimal("2310")
    assert parsed.tp == Decimal("2330")
    assert parsed.all_tps == [Decimal("2330"), Decimal("2340")]


def test_parse_xauusd_sell_without_protection_is_allowed():
    parsed = parse("SELL XAU/USD now")

    assert isinstance(parsed, Signal)
    assert parsed.direction == "SELL"
    assert parsed.symbol == "XAUUSD"
    assert parsed.missing_sl is True
    assert parsed.missing_tp is True


def test_parse_fallback_entry_ignores_sl_tp_prices():
    parsed = parse("BUY EURUSD 1.0850 SL 1.0800 TP 1.0950")

    assert isinstance(parsed, Signal)
    assert parsed.symbol == "EURUSD"
    assert parsed.entry == Decimal("1.0850")
    assert parsed.sl == Decimal("1.0800")
    assert parsed.tp == Decimal("1.0950")


def test_rejects_ambiguous_direction():
    parsed = parse("BUY then SELL GOLD")

    assert isinstance(parsed, ParseFailure)
    assert parsed.reason == "ambiguous_direction"


def test_rejects_non_signal_message():
    parsed = parse("London session starting soon")

    assert isinstance(parsed, ParseFailure)
    assert parsed.reason == "no_action"

