"""Pydantic config models, loading and hashing."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

VARIANT_IDS = (
    "P-LST-VEL",
    "P-LST-BIN",
    "P-NAT-VEL",
    "P-NAT-BIN",
    "D1-BASIS",
    "D2-STATIC",
    "D2-MANAGED",
)


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class RunCfg(_M):
    start: date | None = None
    end: date | None = None
    evidence_tier: str = "reconstructed"


class SourcesCfg(_M):
    binance_vision_base: str = "https://data.binance.vision/data"
    binance_s3_list: str = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
    velocity_base: str = "https://data.velocity.exchange"
    defillama_base: str = "https://yields.llama.fi"
    fred_series: str = "DTB3"
    fred_base: str = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    jupiter_quote: str = "https://lite-api.jup.ag/swap/v1/quote"
    okx_base: str = "https://www.okx.com"


class VelocityCfg(_M):
    symbol: str = "SOL-PERP"
    empty_days_stop: int = 14
    funding_floor_annual: float = 0.1095


class BinanceCfg(_M):
    perp_symbol: str = "SOLUSDT"
    coinm_pair: str = "SOLUSD"
    expiry_hour_utc: int = 8
    include_current_month_daily: bool = True


class StakingCfg(_M):
    lst_pool_id: str = "0e7d0722-9054-4907-8593-567b353c0900"
    pool_fee: float = 0.04
    max_ffill_days: int = 3


class LendingCfg(_M):
    aave_usdc_pool_id: str = "aa70268e-4b52-42bf-a116-608b370f9501"
    kamino_usdc_pool_id: str = "8c4bbc10-5763-5d5b-8450-05ddd24b1c33"
    reserve_yield_source: Literal["none", "aave", "kamino"] = "none"


class MarginCfg(_M):
    perp_im: float = 0.10
    perp_mm: float = 0.05
    coinm_im: float = 0.10
    coinm_mm: float = 0.05


class WindowsCfg(_M):
    T_lst_h: int = 1
    T_native_h: int = 120
    T_transfer_h: int = 24
    T_hold_h: int = 2160
    runup_windows_h: list[int] = Field(default_factory=lambda: [1, 24, 120, 2160])
    rolling_days: list[int] = Field(default_factory=lambda: [7, 30, 90])
    alpha: list[float] = Field(default_factory=lambda: [0.95, 0.99])

    @property
    def gate_alpha(self) -> float:
        return max(self.alpha)


class CostsCfg(_M):
    perp_round_trip_bps: float = 20
    hold_years_assumed: float = 0.5
    resize_bps_per_year: float = 10
    coinm_fee_bps: float = 5
    roll_slippage_bps: float = 5
    lst_swap_bps: float = 10


class DatedCfg(_M):
    dte_min: float = 14
    dte_max: float = 100


class DepthCfg(_M):
    mints: dict[str, str] = Field(
        default_factory=lambda: {
            "jitoSOL": "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
            "mSOL": "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
        }
    )
    sizes: list[float] = Field(default_factory=lambda: [1.0, 10.0, 100.0, 1000.0, 5000.0])
    target_size: float = 1000
    gate_lst: str = "jitoSOL"


class ValidateCfg(_M):
    convergence_tol: float = 0.002
    velocity_cum_tol: float = 1.5e-9


class Thresholds(_M):
    excess_median_min: float = 0.03
    excess_p5_min: float = -0.05
    share_negative_max: float = 0.25
    neg_run_max_days: float = 90
    depth_impact_max: float = -0.005
    min_sample_days: float = 180


class SizingCfg(_M):
    target_per_quarter_usd: float = 1000


class HttpCfg(_M):
    timeout_s: float = 30
    max_parallel_per_host: int = 4
    user_agent: str = "solcarry-research/0.1"


class Config(_M):
    run: RunCfg = Field(default_factory=RunCfg)
    sources: SourcesCfg = Field(default_factory=SourcesCfg)
    velocity: VelocityCfg = Field(default_factory=VelocityCfg)
    binance: BinanceCfg = Field(default_factory=BinanceCfg)
    staking: StakingCfg = Field(default_factory=StakingCfg)
    lending: LendingCfg = Field(default_factory=LendingCfg)
    margin: MarginCfg = Field(default_factory=MarginCfg)
    windows: WindowsCfg = Field(default_factory=WindowsCfg)
    costs: CostsCfg = Field(default_factory=CostsCfg)
    dated: DatedCfg = Field(default_factory=DatedCfg)
    depth: DepthCfg = Field(default_factory=DepthCfg)
    validate_: ValidateCfg = Field(default_factory=ValidateCfg, alias="validate")
    thresholds: Thresholds = Field(default_factory=Thresholds)
    sizing: SizingCfg = Field(default_factory=SizingCfg)
    http: HttpCfg = Field(default_factory=HttpCfg)
    variants: list[str] = Field(default_factory=lambda: list(VARIANT_IDS))

    def model_post_init(self, __context: Any) -> None:
        unknown = [v for v in self.variants if v not in VARIANT_IDS]
        if unknown:
            raise ValueError(f"unknown variants: {unknown}; known: {list(VARIANT_IDS)}")

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def config_hash(cfg: Config) -> str:
    return sha256_text(canonical_json(cfg.dump()))


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text()) or {}
    return Config.model_validate(raw)


class Paths:
    """Filesystem layout rooted at the project directory (parent of config/)."""

    def __init__(self, root: Path, data_dir: Path | None = None) -> None:
        self.root = root
        self.data = data_dir if data_dir is not None else root / "data"
        self.raw = self.data / "raw"
        self.normalized = self.data / "normalized"
        self.results = self.data / "results"
        self.lock_file = root / "config" / "gate1.lock"

    def ensure(self) -> None:
        for p in (self.raw, self.normalized, self.results):
            p.mkdir(parents=True, exist_ok=True)
