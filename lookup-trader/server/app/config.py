from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOOKUP_", env_file=".env", extra="ignore")

    data_dir: Path = _REPO_ROOT / "data"
    candles_glob: str = "candles/**/*.parquet"
    duckdb_path: Path = _REPO_ROOT / "data" / "engine.duckdb"

    max_bars: int = 24
    atr_period: int = 14
    ema_period: int = 200
    rsi_period: int = 14
    ambiguous_policy: str = "conservative"
    labeler_version: str = "1.0.0"
    feature_version: str = "1.0.0"
    min_samples: int = 30

    # Bars of history fetched before the signal bar, independent of the operator's
    # session window. Fixed count is what makes context features reproducible.
    warmup_bars: int = 600

    # Target multiples (in R) scored against the marked SL alongside the marked TP.
    r_grid_targets: list[float] = [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]

    # Round-trip cost assumption per symbol, in pips. Never flips a label; feeds net_r.
    spread_pips: dict[str, float] = {}
    default_spread_pips: float = 0.0

    # UTC session bands (hour inclusive start, exclusive end)
    asian_start: int = 0
    asian_end: int = 7
    london_start: int = 7
    london_end: int = 13
    ny_start: int = 13
    ny_end: int = 21

    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    @property
    def candles_parquet_glob(self) -> str:
        return str(self.data_dir / self.candles_glob)


settings = Settings()
