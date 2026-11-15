"""All settings come from the environment (callisto/.env is loaded if present)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _f(name: str, default: float) -> float:
    v = os.getenv(name, "")
    return float(v) if v.strip() else default


@dataclass(frozen=True)
class Config:
    channel: str = os.getenv("CHANNEL", "callistofx")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
    openai_reasoning: str = os.getenv("OPENAI_REASONING", "low")
    max_images: int = int(_f("MAX_IMAGES_PER_POST", 4))
    min_confidence: float = _f("MIN_CONFIDENCE", 0.6)
    # The channel calls gold almost exclusively; a call with a direction but no visible
    # symbol (blurred screenshot, "SELL NOW") is taken as this ticker. Empty = drop it.
    default_ticker: str = os.getenv("DEFAULT_TICKER", "XAUUSD")


    # Pip size per ticker. Gold "pips" on this channel are $0.10 (500 pips = $5,000/lot).
    pip_xau: float = _f("PIP_XAUUSD", 0.10)

    # Grouping posts into one trade idea.
    group_hours: float = _f("GROUP_HOURS", 6)          # follow-ups within this window
    entry_match_pips: float = _f("ENTRY_MATCH_PIPS", 100)  # same idea if entries this close

    # Trade management (in pips, measured from OUR fill).
    sl_pips: float = _f("SL_PIPS", 60)
    tp_pips: float = _f("TP_PIPS", 150)
    trail_start_pips: float = _f("TRAIL_START_PIPS", 50)   # 0 = no trailing
    trail_distance_pips: float = _f("TRAIL_DISTANCE_PIPS", 30)
    trail_step_pips: float = _f("TRAIL_STEP_PIPS", 10)
    max_hold_hours: float = _f("MAX_HOLD_HOURS", 24)
    entry_delay_seconds: float = _f("ENTRY_DELAY_SECONDS", 30)  # detection + order latency
    max_age_pips: float = _f("MAX_AGE_PIPS", 0)  # skip ideas already this far in profit (0=off)
    lot_usd_per_pip: float = _f("USD_PER_PIP_PER_LOT", 10)

    # Monitor.
    poll_seconds: float = _f("POLL_SECONDS", 15)
    webhook_url: str = os.getenv("WEBHOOK_URL", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    telegram_api_id: str = os.getenv("TELEGRAM_API_ID", "")
    telegram_api_hash: str = os.getenv("TELEGRAM_API_HASH", "")

    data_dir: Path = Path(os.getenv("DATA_DIR", str(ROOT / "data")))

    def pip(self, ticker: str) -> float:
        t = (ticker or "").upper()
        if t.startswith("XAU"):
            return self.pip_xau
        if t.startswith("XAG"):
            return 0.01
        if t.endswith("JPY"):
            return 0.01
        return 0.0001


CFG = Config()
CFG.data_dir.mkdir(parents=True, exist_ok=True)
