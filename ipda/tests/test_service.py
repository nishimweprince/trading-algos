from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ipda.candles import Candle
from ipda.config import Settings
from ipda.data_client import Tick
from ipda.logging_config import RuntimeLogs
from ipda.mt5_client import SubmitOutcome
from ipda.position_tracker import PositionTracker
from ipda.service import SignalService

# A time inside the New York window (13:00 UTC = 08:00 EST) and one outside both.
IN_SESSION = datetime(2026, 1, 14, 15, 0, tzinfo=UTC)
OUT_OF_SESSION = datetime(2026, 1, 14, 11, 0, tzinfo=UTC)


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "DATA_API_URL": "http://127.0.0.1:8000/v1/market-data/candles",
        "QUOTE": "EURUSD",
        "MT5_SYMBOL": "EURUSD",
        "VOLUME": "0.10",
        "MT5_SIGNAL_API_KEY": "test-api-key-with-16-characters",
        "LOGS_DIR": str(tmp_path / "logs"),
        "USE_HARD_TARGETS": True,
        "NOTIFICATIONS_ENABLED": True,
    }
    base.update(overrides)
    settings = Settings(**base)
    from ipda.instruments import instrument_from_legacy

    return settings.model_copy(update={"instruments": [instrument_from_legacy("EURUSD", "EURUSD")]})


def _swing_minutes(n: int) -> list[Candle]:
    """A sawtooth: long slides that drive RSI(14) under 25, then sharp rallies that
    cross it back up — a repeating Buy Chance.

    The slide has to run well past the 14-bar RSI window for the reading to actually
    reach 25 — a shallower sawtooth keeps enough average gain in the RMA that RSI
    never gets there. The 95-minute cycle is deliberately not a multiple of any
    plausible ``TARGET_TF_MINUTES``, so successive crossings land at different offsets
    within their bucket. That is what lets a test ask for a crossing on a *partial*
    candle.
    """
    candles: list[Candle] = []
    close = 5000.0
    prev = close
    start = datetime(2026, 1, 14, 8, 0, tzinfo=UTC)
    for i in range(n):
        close += -1.0 if i % 95 < 70 else 3.0
        candles.append(
            Candle(
                start=start + timedelta(minutes=i),
                open=prev,
                high=max(prev, close) + 0.3,
                low=min(prev, close) - 0.3,
                close=close,
                volume=1.0,
            )
        )
        prev = close
    return candles


def _minutes_that_fire(
    settings: Settings, direction: str, minutes_into_bucket: int | None = None
) -> list[Candle]:
    """Shortest swing series whose *forming* bucket produces ``direction``.

    Signals fire on the forming bar, so the crossing has to land on the last
    bucket — searching for that truncation is more honest than hand-tuning a
    waveform until it happens to work.

    ``minutes_into_bucket`` additionally constrains how far into that bucket the
    feed stops, so a test can demand a genuinely partial candle. It cannot simply
    trim a series found without the constraint: dropping minutes changes the
    bucket's close, and with it the RSI reading that produced the signal.
    """
    from ipda.candles import Aggregator
    from ipda.service import _InstrumentPipeline

    aggregator = Aggregator(settings.target_tf_minutes, settings.bucket_offset_minutes)
    pipeline = _InstrumentPipeline(settings.instruments[0], settings)
    full = _swing_minutes(900)

    for length in range(200, len(full)):
        series = aggregator.build(full[:length])
        if minutes_into_bucket is not None:
            filled = sum(1 for m in full[:length] if m.start >= series.forming.start)
            if filled != minutes_into_bucket:
                continue
        decision = pipeline._strategy.evaluate(series)
        if decision is not None and decision.direction == direction:
            return full[:length]
    raise AssertionError(f"no {direction} signal in the fixture series")


class FakeDataClient:
    def __init__(self, minutes: list[Candle], tick: Tick | None = None) -> None:
        self._minutes = minutes
        self._tick = tick or Tick("EURUSD", bid=100.0, ask=100.0)
        self.tick_calls: list[str] = []

    async def fetch_minute_candles(self, quote: str) -> list[Candle]:
        return self._minutes

    async def fetch_tick(self, quote: str) -> Tick:
        self.tick_calls.append(quote)
        return self._tick


class FakeMt5Client:
    def __init__(self, outcome: SubmitOutcome | None = None) -> None:
        self.submitted: list[dict[str, Any]] = []
        self._outcome = outcome or SubmitOutcome(
            kind="success", status_code=200, detail={"execution_price": "100.5"}
        )

    async def submit(self, payload: dict[str, Any]) -> SubmitOutcome:
        self.submitted.append(payload)
        return self._outcome


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, list[str]]] = []

    async def send(self, subject: str, lines: list[str], **context: Any) -> None:
        self.sent.append((subject, lines))


def _service(
    tmp_path: Path,
    settings: Settings,
    data: FakeDataClient,
    mt5: FakeMt5Client,
    notifier: FakeNotifier,
    tracker: PositionTracker | None = None,
) -> SignalService:
    return SignalService(
        settings=settings,
        data_client=data,  # type: ignore[arg-type]
        mt5_client=mt5,  # type: ignore[arg-type]
        logs=RuntimeLogs(settings.logs_dir),
        notifier=notifier,  # type: ignore[arg-type]
        tracker=tracker,
    )


def _freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    import ipda.service as service_module

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return moment

    monkeypatch.setattr(service_module, "datetime", _FrozenDatetime)


def _signal_kinds(settings: Settings) -> list[str]:
    import json

    path = settings.logs_dir / "signals.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line)["kind"] for line in path.read_text().splitlines() if line]


async def test_in_session_signal_is_submitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, IN_SESSION)
    settings = _settings(tmp_path)
    data = FakeDataClient(_minutes_that_fire(settings, "buy"))
    mt5, notifier = FakeMt5Client(), FakeNotifier()

    await _service(tmp_path, settings, data, mt5, notifier).tick()

    assert len(mt5.submitted) == 1
    assert mt5.submitted[0]["symbol"] == "EURUSD"
    assert notifier.sent == []
    assert "signal_fired" in _signal_kinds(settings)


async def test_out_of_session_signal_notifies_instead_of_trading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, OUT_OF_SESSION)
    settings = _settings(tmp_path)
    data = FakeDataClient(_minutes_that_fire(settings, "buy"))
    mt5, notifier = FakeMt5Client(), FakeNotifier()

    await _service(tmp_path, settings, data, mt5, notifier).tick()

    assert mt5.submitted == []
    assert len(notifier.sent) == 1
    subject, lines = notifier.sent[0]
    assert "not executed" in subject
    assert any("trading sessions: tokyo, new_york" in line for line in lines)
    assert "signal_skipped_out_of_session" in _signal_kinds(settings)


async def test_empty_sessions_trade_around_the_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, OUT_OF_SESSION)
    settings = _settings(tmp_path, TRADING_SESSIONS="")
    data = FakeDataClient(_minutes_that_fire(settings, "buy"))
    mt5, notifier = FakeMt5Client(), FakeNotifier()

    await _service(tmp_path, settings, data, mt5, notifier).tick()

    assert len(mt5.submitted) == 1
    assert notifier.sent == []


async def test_signal_fires_once_per_candle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, IN_SESSION)
    settings = _settings(tmp_path)
    data = FakeDataClient(_minutes_that_fire(settings, "buy"))
    mt5, notifier = FakeMt5Client(), FakeNotifier()
    service = _service(tmp_path, settings, data, mt5, notifier)

    await service.tick()
    await service.tick()

    assert len(mt5.submitted) == 1


async def test_out_of_session_signal_notifies_once_per_candle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bucket lock must apply to skipped signals too, or every poll re-notifies."""
    _freeze(monkeypatch, OUT_OF_SESSION)
    settings = _settings(tmp_path)
    data = FakeDataClient(_minutes_that_fire(settings, "buy"))
    mt5, notifier = FakeMt5Client(), FakeNotifier()
    service = _service(tmp_path, settings, data, mt5, notifier)

    await service.tick()
    await service.tick()
    await service.tick()

    assert len(notifier.sent) == 1


async def test_fill_starts_break_even_tracking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, IN_SESSION)
    settings = _settings(tmp_path, PIP_SIZE=0.0001)
    tracker = PositionTracker(tmp_path / "open_trades.json", break_even_pips=30.0, ttl_hours=24.0)
    data = FakeDataClient(_minutes_that_fire(settings, "buy"))
    mt5, notifier = FakeMt5Client(), FakeNotifier()

    await _service(tmp_path, settings, data, mt5, notifier, tracker).tick()

    assert len(tracker.trades) == 1
    assert tracker.trades[0].entry == 100.5  # the broker fill, not the bar close


async def test_break_even_notification_is_sent_and_not_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, IN_SESSION)
    settings = _settings(tmp_path, PIP_SIZE=0.01)
    tracker = PositionTracker(tmp_path / "open_trades.json", break_even_pips=30.0, ttl_hours=24.0)
    notifier = FakeNotifier()
    mt5 = FakeMt5Client()
    # A buy filled at 100.5; bid 100.8 is +30 pips at PIP_SIZE=0.01.
    minutes = _minutes_that_fire(settings, "buy")
    data = FakeDataClient(minutes, tick=Tick("EURUSD", bid=100.80, ask=100.82))
    service = _service(tmp_path, settings, data, mt5, notifier, tracker)

    await service.tick()  # fills and starts tracking
    assert [s for s, _ in notifier.sent] == []

    await service.tick()  # samples the price, crosses the trigger
    subjects = [s for s, _ in notifier.sent]
    assert len(subjects) == 1
    assert "move stop to break-even" in subjects[0]

    await service.tick()
    assert len([s for s, _ in notifier.sent]) == 1


async def test_tick_poll_failure_is_logged_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, IN_SESSION)
    settings = _settings(tmp_path)
    tracker = PositionTracker(tmp_path / "open_trades.json", break_even_pips=30.0, ttl_hours=24.0)

    class BrokenTick(FakeDataClient):
        async def fetch_tick(self, quote: str) -> Tick:
            raise RuntimeError("tick endpoint down")

    data = BrokenTick(_minutes_that_fire(settings, "buy"))
    service = _service(tmp_path, settings, data, FakeMt5Client(), FakeNotifier(), tracker)

    await service.tick()  # opens a tracked trade
    await service.tick()  # tick fetch fails; must not propagate

    assert len(tracker.trades) == 1


async def test_signal_executes_mid_formation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An order must go out while the target candle is still open.

    The feed stops one minute into a 3M bucket, so the bar the signal fires on holds a
    single 1M candle and is two minutes from closing. If evaluation waited for a closed
    bar this would submit nothing.
    """
    _freeze(monkeypatch, IN_SESSION)
    settings = _settings(tmp_path)
    minutes = _minutes_that_fire(settings, "buy", minutes_into_bucket=1)

    data = FakeDataClient(minutes)
    mt5, notifier = FakeMt5Client(), FakeNotifier()
    await _service(tmp_path, settings, data, mt5, notifier).tick()

    assert len(mt5.submitted) == 1
    assert notifier.sent == []

    import json

    fired = json.loads((settings.logs_dir / "signals.jsonl").read_text().splitlines()[0])
    # Stamped with the still-open bucket, and the feed ends on that bucket's first minute.
    bucket_start = datetime.fromisoformat(fired["bucket_start"])
    assert minutes[-1].start == bucket_start
    assert minutes[-1].start.minute % settings.target_tf_minutes == 0
