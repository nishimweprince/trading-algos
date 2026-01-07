"""Instrument name mapping utility for converting between formats"""
from typing import Dict, Optional
from loguru import logger


class InstrumentMapper:
    """Utility for instrument name conversion between different formats"""
    
    # Mapping from standard format (EUR_USD) to Capital.com epics
    # Capital.com uses different formats:
    # - Forex: Simple 6-character format (EURUSD)
    # - Commodities: Longer format (CS.D.XAUUSD.CFD.IP or similar)
    EPIC_MAP: Dict[str, str] = {
        # Forex pairs (6-character format)
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
        # Commodities (longer format)
        'XAU_USD': 'CS.D.XAUUSD.CFD.IP',  # Gold (most common Capital.com format)
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
        """
        Convert standard format (EUR_USD or EURUSD) to Capital.com epic.
        
        Supports both forex (6-character) and commodities (longer format).
        """
        # Check if already a valid epic (could be 6-char forex or longer commodity format)
        if cls.is_valid_epic(instrument):
            return instrument

        # Try direct mapping first (handles both forex and commodities)
        if instrument in cls.EPIC_MAP:
            return cls.EPIC_MAP[instrument]

        # Try to construct epic from instrument name
        # Format: EUR_USD -> EURUSD or EURUSD -> EURUSD (passthrough)
        try:
            # Handle format with underscore: EUR_USD
            if '_' in instrument:
                base, quote = instrument.split('_')
                # For forex pairs, construct 6-character epic
                if len(base) == 3 and len(quote) == 3:
                    epic = f"{base}{quote}"
                    logger.info(f"Mapped {instrument} to {epic} (constructed from underscore format)")
                    return epic
                else:
                    # For non-standard pairs (like XAU_USD), try to find in map or construct
                    logger.warning(f"Non-standard instrument format: {instrument}. Add to EPIC_MAP if needed.")
                    return instrument

            # Handle format without underscore: EURUSD (6 characters) - already correct format
            elif len(instrument) == 6 and instrument.isalpha():
                logger.debug(f"Using {instrument} as-is (already in epic format)")
                return instrument.upper()

            else:
                logger.warning(f"Could not parse instrument format: {instrument} (expected EUR_USD or EURUSD, or add to EPIC_MAP)")
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
        """
        Validate if string is a valid Capital.com epic format.
        
        Capital.com uses different epic formats:
        - Forex: 6-character format (EURUSD)
        - Commodities: Longer format (CS.D.XAUUSD.CFD.IP)
        - Other instruments: Various formats
        """
        # Allow both 6-character forex epics and longer commodity/instrument epics
        if len(epic) == 6 and epic.isalpha() and epic.isupper():
            return True
        # Allow longer epics (for commodities, indices, etc.)
        if len(epic) > 6 and '.' in epic:
            return True
        return False
    
    @classmethod
    def add_mapping(cls, instrument: str, epic: str) -> None:
        """Add a custom instrument to epic mapping"""
        cls.EPIC_MAP[instrument] = epic
        cls._REVERSE_MAP = None  # Reset reverse map to force regeneration

