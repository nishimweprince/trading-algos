from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVER_ROOT = _REPO_ROOT / "server"


class Settings(BaseSettings):
    # Resolve the private file from the repository, not the caller's cwd.  Root
    # CLIs and the API therefore read the same ignored settings without putting
    # secrets on command lines.
    model_config = SettingsConfigDict(
        env_prefix="LOOKUP_", env_file=_SERVER_ROOT / ".env", extra="ignore"
    )

    data_dir: Path = _REPO_ROOT / "data"
    candles_glob: str = "candles/**/*.parquet"
    duckdb_path: Path = _REPO_ROOT / "data" / "engine.duckdb"
    outcome_artifact_root: Path = _REPO_ROOT / "data" / "models" / "outcome"
    outcome_artifact_version: str = "xauusd-h1-outcome-v1-pilot-20260805-r2"
    # Derived from candles and regenerable, so it lives beside them rather than
    # in the DuckDB file, which stays reserved for mutable operator state.
    features_dirname: str = "features"

    # Capital.com is used only for post-HistData market data.  Capital does not
    # issue read-only keys, so the provider deliberately exposes no trading
    # methods even though these credentials are trading-capable.
    capital_environment: Literal["demo", "live"] = "demo"
    capital_api_key: SecretStr | None = None
    capital_identifier: SecretStr | None = None
    capital_api_password: SecretStr | None = None
    # Canonical app/training symbol -> provider-specific Capital EPIC. Keep the
    # provider name at this boundary; persisted candles and models use XAUUSD.
    capital_epics: dict[str, str] = {"XAUUSD": "GOLD"}
    capital_price_side: Literal["bid"] = "bid"
    capital_overlap_bars: int = 3
    capital_settlement_seconds: int = 90
    capital_poll_seconds: int = 60
    shadow_db_path: Path = _REPO_ROOT / "data" / "shadow.sqlite3"
    health_parity_sample_size: int = 5

    max_bars: int = 24
    atr_period: int = 14
    ema_period: int = 200
    rsi_period: int = 14
    ambiguous_policy: str = "conservative"
    labeler_version: str = "1.0.0"
    feature_version: str = "3.0.0"
    min_samples: int = 30

    # Bars of history fetched before the signal bar, independent of the operator's
    # session window. Fixed count is what makes context features reproducible.
    warmup_bars: int = 600

    # Target multiples (in R) scored against the marked SL alongside the marked TP.
    r_grid_targets: list[float] = [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]

    # Round-trip cost assumption per symbol, in pips. Never flips a label; feeds net_r.
    spread_pips: dict[str, float] = {"XAUUSD": 3.0, "EURUSD": 0.8}
    default_spread_pips: float = 0.0

    # Currencies whose high-impact events matter for a symbol (Forex Factory).
    calendar_symbol_currencies: dict[str, list[str]] = {
        "XAUUSD": ["USD", "EUR", "CNY"],
        "EURUSD": ["EUR", "USD"],
        "GBPUSD": ["GBP", "USD"],
        "USDJPY": ["USD", "JPY"],
    }
    calendar_impact_hours: int = 2

    # Cut points for the comparison buckets derived at write time.
    # Planned reward/risk -> low | standard | high
    rr_buckets: tuple[float, float] = (1.5, 2.5)
    # Stop distance in ATR units -> tight | normal | wide
    sl_atr_buckets: tuple[float, float] = (0.75, 1.5)

    # UTC session bands (hour inclusive start, exclusive end)
    asian_start: int = 0
    asian_end: int = 7
    london_start: int = 7
    london_end: int = 13
    ny_start: int = 13
    ny_end: int = 21

    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Higher timeframe used for computed HTF trend (LTF -> HTF).
    htf_map: dict[str, str] = {
        "M5": "M15",
        "M15": "H1",
        "M30": "H1",
        "H1": "H4",
        "H4": "D1",
        "D1": "W1",
    }

    ema_slope_lookback: int = 10
    ema_slope_buckets: tuple[float, float] = (-0.05, 0.05)
    atr_change_lookback: int = 20
    atr_change_buckets: tuple[float, float] = (0.9, 1.1)
    # Swing confirmation lives in `app.taggers.thresholds.SWING_LOOKBACK`, not
    # here: it decides which pivots exist, so an env override would change what
    # every chart tag means without invalidating the store.

    # --- Bar feature store -------------------------------------------------
    # Bumping this invalidates the store: the builder rewrites rows whose version
    # differs, so re-cutting a threshold is a rebuild rather than a migration.
    bar_feature_version: str = "1.4.0"

    # Automated meta-event contracts. These are intentionally independent of
    # the legacy outcome model so the two datasets cannot be confused.
    meta_feature_version: int = 2
    # 2: barriers widened to a 2 ATR stop / 3 ATR target, and every `_r` column
    # restated in R (multiples of the stop) rather than in ATR. The event
    # population is unchanged, so the export stays `meta_events_v1`; only the
    # labels attached to those events moved.
    meta_label_version: int = 2
    meta_event_manifest_version: int = 2

    # Immutable research-shadow artifacts. This namespace is deliberately
    # separate from the incompatible legacy three-class outcome model.
    meta_artifact_root: Path = _REPO_ROOT / "data" / "models" / "meta"
    meta_shadow_db_path: Path = _REPO_ROOT / "data" / "meta_shadow.sqlite3"

    # Forward horizons scored per bar. Excursions are stored per horizon because
    # max over 24 bars is not recoverable from max over 48.
    feature_horizons: list[int] = [6, 12, 24, 48]

    # Barrier distances in ATR units. Stored as bars-to-first-touch up and down,
    # from which any (target, stop, horizon, side) outcome is arithmetic.
    touch_levels: list[float] = [0.5, 1.0, 1.5, 2.0, 3.0]

    # Trailing window kept as an ATR-normalised shape vector, plus the coarse
    # downsample used for filtering and similarity.
    shape_back_bars: int = 120
    shape_downsample_groups: int = 12

    efficiency_ratio_lookback: int = 20
    close_range_lookback: int = 50
    volume_z_lookback: int = 50

    # London/NY overlap, which the three-band session split folds into "ny".
    session_overlap_start: int = 13
    session_overlap_end: int = 16

    # Bars, not trades — but adjacent bars share their forward window, so this is
    # deliberately far above min_samples and the interval is still widened to the
    # effective (non-overlapping) count.
    base_rate_min_samples: int = 200
    # A context needs evidence across enough distinct market weeks that one
    # short regime cannot masquerade as a repeatable edge.
    base_rate_min_periods: int = 20
    base_rate_bootstrap_samples: int = 2000

    @property
    def candles_parquet_glob(self) -> str:
        return str(self.data_dir / self.candles_glob)

    @property
    def features_dir(self) -> Path:
        return self.data_dir / self.features_dirname


settings = Settings()
