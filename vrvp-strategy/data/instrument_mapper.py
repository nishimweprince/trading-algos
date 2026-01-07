"""Instrument name mapping utility for converting between formats"""
from typing import Dict, Optional
from loguru import logger


class InstrumentMapper:
    """Utility for instrument name conversion between different formats"""
    
    # Mapping from standard format (EUR_USD) to Capital.com epics
    # Capital.com uses simple format: EURUSD (no separators)
    EPIC_MAP: Dict[str, str] = {
        'EUR_USD': 'EURUSD',
        'GBP_USD': 'GBPUSD',
        'USD_JPY': 'USDJPY',
        'USD_CHF': 'USDCHF',
        'AUD_USD': 'AUDUSD',
        'USD_CAD': 'USDCAD',
        'NZD_USD': 'NZDUSD',
        'EUR_GBP': 'EURGBP',
        'EUR_JPY': 'EURJPY',
        'GBP_JPY': 'GBPJPY',
        'AUD_JPY': 'AUDJPY',
        'EUR_CHF': 'EURCHF',
        'AUD_NZD': 'AUDNZD',
        'EUR_AUD': 'EURAUD',
        'GBP_AUD': 'GBPAUD',
    }
    
    # Reverse mapping from epics to standard format
    _REVERSE_MAP: Optional[Dict[str, str]] = None
    
    @classmethod
    def _get_reverse_map(cls) -> Dict[str, str]:
        """Lazy initialization of reverse mapping"""
        if cls._REVERSE_MAP is None:
            cls._REVERSE_MAP = {v: k for k, v in cls.EPIC_MAP.items()}
        return cls._REVERSE_MAP
    
    @classmethod
    def to_capitalcom_epic(cls, instrument: str) -> str:
        """Convert standard format (EUR_USD or EURUSD) to Capital.com epic (EURUSD)"""
        # Check if already a valid epic (6-character format)
        if cls.is_valid_epic(instrument):
            return instrument

        # Try direct mapping
        if instrument in cls.EPIC_MAP:
            return cls.EPIC_MAP[instrument]

        # Try to construct epic from instrument name
        # Format: EUR_USD -> EURUSD or EURUSD -> EURUSD (passthrough)
        try:
            # Handle format with underscore: EUR_USD
            if '_' in instrument:
                base, quote = instrument.split('_')
                epic = f"{base}{quote}"
                logger.info(f"Mapped {instrument} to {epic} (constructed from underscore format)")
                return epic

            # Handle format without underscore: EURUSD (6 characters) - already correct format
            elif len(instrument) == 6 and instrument.isalpha():
                logger.debug(f"Using {instrument} as-is (already in epic format)")
                return instrument.upper()

            else:
                logger.warning(f"Could not parse instrument format: {instrument} (expected EUR_USD or EURUSD)")
                return instrument

        except Exception as e:
            logger.warning(f"Could not parse instrument format: {instrument} - {e}")
            return instrument
    
    @classmethod
    def from_capitalcom_epic(cls, epic: str) -> str:
        """Convert Capital.com epic to standard format (EURUSD -> EUR_USD)"""
        # Check reverse mapping
        reverse_map = cls._get_reverse_map()
        if epic in reverse_map:
            return reverse_map[epic]

        # Try to parse epic format: EURUSD -> EUR_USD
        try:
            if len(epic) == 6 and epic.isalpha():
                # Split into base and quote (assuming 3-letter codes)
                base = epic[:3]
                quote = epic[3:]
                standard = f"{base}_{quote}"
                logger.debug(f"Mapped {epic} to {standard} (parsed)")
                return standard
        except Exception as e:
            logger.warning(f"Could not parse epic format: {epic} - {e}")

        return epic

    @classmethod
    def is_valid_epic(cls, epic: str) -> bool:
        """Validate if string is a valid Capital.com epic format"""
        # Capital.com epics are simple 6-character currency pairs: EURUSD, GBPUSD, etc.
        return len(epic) == 6 and epic.isalpha() and epic.isupper()
    
    @classmethod
    def add_mapping(cls, instrument: str, epic: str) -> None:
        """Add a custom instrument to epic mapping"""
        cls.EPIC_MAP[instrument] = epic
        cls._REVERSE_MAP = None  # Reset reverse map to force regeneration

