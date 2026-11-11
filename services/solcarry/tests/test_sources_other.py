from __future__ import annotations

from datetime import date, timedelta

import httpx
import respx

from solcarry.http import Http
from solcarry.sources.defillama import DefiLlama, parse_chart
from solcarry.sources.fred import parse_fred_csv
from solcarry.sources.jupiter import Jupiter, parse_quote
from solcarry.sources.velocity import VELOCITY_COLS, Velocity, parse_csv

VBASE = "https://data.velocity.exchange/market/SOL-PERP/fundingRates"


def test_velocity_parse(fixtures):
    df = parse_csv((fixtures / "velocity_day.csv").read_bytes())
    assert list(df.columns) == VELOCITY_COLS
    assert len(df) == 3
    assert df["fundingRate"].dtype.kind == "f"


@respx.mock
def test_velocity_start_discovery(cfg, paths, fixtures):
    """Days >= start have rows; earlier days are empty. Stops after N empty days."""
    cfg.velocity.empty_days_stop = 3
    today = date(2026, 7, 15)
    start = date(2026, 7, 10)
    body = (fixtures / "velocity_day.csv").read_text()
    header = body.splitlines()[0] + "\n"
    calls = []

    def handler(request):
        y, m, d = request.url.path.split("/")[-3:]
        day = date(int(y), int(m), int(d))
        calls.append(day)
        return httpx.Response(200, text=body if day >= start else header)

    respx.get(url__startswith=VBASE + "/").mock(side_effect=handler)
    v = Velocity(cfg, Http(cfg.http), paths.raw)
    assert v.discover_start(today) == start
    assert min(calls) == start - timedelta(days=3)
    assert len(calls) == (today - start).days + 1 + 3
    # cached start is reused without requests
    respx.reset()
    assert v.discover_start(today) == start


def test_defillama_parse(fixtures):
    df = parse_chart((fixtures / "defillama_chart.json").read_bytes())
    assert len(df) == 2 and df["apyBase"].iloc[0] == 7.61
    assert str(df["timestamp"].dt.tz) == "UTC"


@respx.mock
def test_defillama_fetch(cfg, paths, fixtures):
    respx.get(url__startswith="https://yields.llama.fi/chart/").mock(
        return_value=httpx.Response(200, content=(fixtures / "defillama_chart.json").read_bytes())
    )
    dl = DefiLlama(cfg, Http(cfg.http), paths.raw)
    assert dl.sync() == {"jitosol": 2, "aave_usdc": 2, "kamino_usdc": 2}
    assert len(dl.load("jitosol")) == 2


def test_fred_dot_handling(fixtures):
    df = parse_fred_csv((fixtures / "fred.csv").read_bytes(), "DTB3")
    assert len(df) == 3
    assert df["DTB3"].isna().sum() == 1


def test_jupiter_parse(fixtures):
    import json

    q = parse_quote(json.loads((fixtures / "jupiter_quote.json").read_text()))
    assert q.out_amount / 1e9 == 1309.53
    assert q.routes == "Manifest:60% + Jito:40%"
    assert q.price_impact_pct == 0.0012


@respx.mock
def test_jupiter_quote_failure(cfg):
    respx.get(url__startswith="https://lite-api.jup.ag/").mock(return_value=httpx.Response(400))
    assert Jupiter(cfg, Http(cfg.http)).quote("x", 1) is None


@respx.mock
def test_http_retries_on_5xx(cfg):
    route = respx.get("https://example.test/x").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, text="ok")]
    )
    h = Http(cfg.http, backoff_max_s=0.01)
    assert h.get("https://example.test/x").text == "ok"
    assert route.call_count == 2
