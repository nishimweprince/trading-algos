from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOOKUP_", env_file=".env", extra="ignore")

    data_dir: Path = _REPO_ROOT / "data"
    candles_glob: str = "candles/**/*.parquet"
    duckdb_path: Path = _REPO_ROOT / "data" / "engine.duckdb"
    outcome_artifact_root: Path = _REPO_ROOT / "data" / "models" / "outcome"
    outcome_artifact_version: str = "xauusd-h1-outcome-v1-pilot-20260805-r2"
    # Derived from candles and regenerable, so it lives beside them rather than
    # in the DuckDB file, which stays reserved for mutable operator state.
    features_dirname: str = "features"

    # Read-only OANDA v20 market-data access. Credentials are accepted only
    # through LOOKUP_ environment/settings inputs and are never CLI arguments.
    oanda_environment: Literal["practice", "live"] = "practice"
    oanda_token: SecretStr | None = None
    oanda_account_id: SecretStr | None = None
    oanda_overlap_bars: int = 3
    oanda_initial_backfill_days: int = 30
    health_parity_sample_size: int = 5

    max_bars: int = 24
    atr_period: int = 14
    ema_period: int = 200
    rsi_period: int = 14
    ambiguous_policy: str = "conservative"
    labeler_version: str = "1.0.0"
    feature_version: str = "2.0.0"
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
        "XAUUSD": ["USD"],
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
    swing_lookback: int = 5

    # --- Bar feature store -------------------------------------------------
    # Bumping this invalidates the store: the builder rewrites rows whose version
    # differs, so re-cutting a threshold is a rebuild rather than a migration.
    bar_feature_version: str = "1.2.0"

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

    @property
    def candles_parquet_glob(self) -> str:
        return str(self.data_dir / self.candles_glob)

    @property
    def features_dir(self) -> Path:
        return self.data_dir / self.features_dirname


settings = Settings()
