"""Instrument name mapping utility for converting between formats.

Capital.com Epic Format Reference:
- Forex: {BASE}{QUOTE} concatenated, uppercase (e.g., EURUSD)
- Commodities: Simple uppercase names (e.g., GOLD, SILVER, OIL_CRUDE)
- Indices: Region + number format (e.g., US500, UK100, DE40)
- Cryptocurrencies: {CRYPTO}{FIAT} concatenated (e.g., BTCUSD)
- Stocks: Standard exchange ticker symbols (e.g., AAPL, TSLA)

Demo and live environments use identical epic formats; only the base URL differs.
For instruments not in this mapping, use the search_markets() API to discover epics.
"""
from typing import Dict, Optional
from loguru import logger


class InstrumentMapper:
    """Utility for instrument name conversion between different formats.

    Capital.com uses simple, human-readable epic identifiers—not the complex
    multi-part formats used by platforms like IG Markets.
    """

    # Mapping from standard format (EUR_USD) to Capital.com epics
    # Capital.com uses simple uppercase formats:
    # - Forex: EURUSD (concatenated, no separators)
    # - Commodities: GOLD, SILVER, OIL_CRUDE (natural names)
    # - Indices: US500, UK100, DE40 (region + number)
    # - Crypto: BTCUSD, ETHUSD (crypto + fiat)
    # - Stocks: AAPL, TSLA (ticker symbols)
    EPIC_MAP: Dict[str, str] = {
        # ============================================================
        # FOREX - Major Pairs (instrumentType: "CURRENCIES")
        # Pattern: {BASE}{QUOTE} - no separators, uppercase
        # ============================================================
        'EUR_USD': 'EURUSD',
        'GBP_USD': 'GBPUSD',
        'USD_JPY': 'USDJPY',
        'USD_CHF': 'USDCHF',
        'AUD_USD': 'AUDUSD',
        'USD_CAD': 'USDCAD',
        'NZD_USD': 'NZDUSD',

        # FOREX - Cross Pairs
        'EUR_GBP': 'EURGBP',
        'EUR_JPY': 'EURJPY',
        'GBP_JPY': 'GBPJPY',
        'AUD_JPY': 'AUDJPY',
        'EUR_CHF': 'EURCHF',
        'AUD_NZD': 'AUDNZD',
        'EUR_AUD': 'EURAUD',
        'GBP_AUD': 'GBPAUD',
        'EUR_CAD': 'EURCAD',
        'GBP_CAD': 'GBPCAD',
        'CHF_JPY': 'CHFJPY',
        'CAD_JPY': 'CADJPY',
        'NZD_JPY': 'NZDJPY',
        'GBP_CHF': 'GBPCHF',
        'AUD_CAD': 'AUDCAD',
        'AUD_CHF': 'AUDCHF',
        'NZD_CAD': 'NZDCAD',
        'NZD_CHF': 'NZDCHF',
        'EUR_NZD': 'EURNZD',
        'GBP_NZD': 'GBPNZD',

        # ============================================================
        # COMMODITIES (instrumentType: "COMMODITIES")
        # Pattern: Simple uppercase names, underscores for multi-word
        # ============================================================
        # Precious Metals
        'XAU_USD': 'GOLD',
        'XAG_USD': 'SILVER',
        'GOLD': 'GOLD',
        'SILVER': 'SILVER',

        # Energy
        'WTI': 'OIL_CRUDE',
        'OIL_WTI': 'OIL_CRUDE',
        'CRUDE_OIL': 'OIL_CRUDE',
        'BRENT': 'OIL_BRENT',
        'OIL_BRENT': 'OIL_BRENT',
        'NATURAL_GAS': 'NATURALGAS',
        'NATGAS': 'NATURALGAS',

        # Other Commodities
        'COPPER': 'COPPER',
        'PLATINUM': 'PLATINUM',
        'PALLADIUM': 'PALLADIUM',

        # ============================================================
        # INDICES (instrumentType: "INDICES")
        # Pattern: Short country/region codes with number
        # ============================================================
        # US Indices
        'SP500': 'US500',
        'US500': 'US500',
        'SPX': 'US500',
        'NASDAQ': 'US100',
        'US100': 'US100',
        'NDX': 'US100',
        'DOW': 'US30',
        'US30': 'US30',
        'DJI': 'US30',
        'RUSSELL2000': 'RTY',
        'RTY': 'RTY',
        'VIX': 'VIX',

        # European Indices
        'DAX': 'DE40',
        'DE40': 'DE40',
        'FTSE100': 'UK100',
        'UK100': 'UK100',
        'CAC40': 'FR40',
        'FR40': 'FR40',
        'EUROSTOXX50': 'EU50',
        'EU50': 'EU50',

        # Asian Indices
        'NIKKEI225': 'J225',
        'J225': 'J225',
        'HANGSENG': 'HK50',
        'HK50': 'HK50',
        'CHINAA50': 'CN50',
        'CN50': 'CN50',
        'ASX200': 'AU200',
        'AU200': 'AU200',

        # ============================================================
        # CRYPTOCURRENCIES (instrumentType: "CRYPTOCURRENCIES")
        # Pattern: {CRYPTO}{FIAT} - typically paired with USD
        # ============================================================
        'BTC_USD': 'BTCUSD',
        'BTCUSD': 'BTCUSD',
        'ETH_USD': 'ETHUSD',
        'ETHUSD': 'ETHUSD',
        'XRP_USD': 'XRPUSD',
        'XRPUSD': 'XRPUSD',
        'LTC_USD': 'LTCUSD',
        'LTCUSD': 'LTCUSD',
        'ADA_USD': 'ADAUSD',
        'ADAUSD': 'ADAUSD',
        'DOT_USD': 'DOTUSD',
        'DOTUSD': 'DOTUSD',
        'SOL_USD': 'SOLUSD',
        'SOLUSD': 'SOLUSD',
        'DOGE_USD': 'DOGEUSD',
        'DOGEUSD': 'DOGEUSD',
        'AVAX_USD': 'AVAXUSD',
        'LINK_USD': 'LINKUSD',
        'MATIC_USD': 'MATICUSD',

        # ============================================================
        # POPULAR STOCKS (instrumentType: "SHARES")
        # Pattern: Standard exchange ticker symbols
        # ============================================================
        'AAPL': 'AAPL',
        'TSLA': 'TSLA',
        'NVDA': 'NVDA',
        'MSFT': 'MSFT',
        'GOOGL': 'GOOGL',
        'AMZN': 'AMZN',
        'META': 'META',
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

        Capital.com uses simple uppercase formats:
        - Forex: EURUSD (6-char concatenated)
        - Commodities: GOLD, SILVER, OIL_CRUDE
        - Indices: US500, UK100, DE40
        - Crypto: BTCUSD, ETHUSD
        - Stocks: AAPL, TSLA

        IMPORTANT: Always check EPIC_MAP first before constructing epics,
        as some instruments (like XAU_USD) need explicit mapping.
        """
        # Normalize input to uppercase for consistent handling
        instrument_upper = instrument.upper().strip()
        
        # CRITICAL: Check EPIC_MAP FIRST before any other logic
        # This ensures explicit mappings (like XAU_USD -> GOLD) are used
        if instrument_upper in cls.EPIC_MAP:
            mapped_epic = cls.EPIC_MAP[instrument_upper]
            logger.debug(f"Mapped {instrument} to {mapped_epic} via EPIC_MAP")
            return mapped_epic

        # Check if already a valid Capital.com epic (and not in EPIC_MAP as a key)
        # This handles cases where user passes the epic directly (e.g., 'GOLD', 'EURUSD')
        if cls.is_valid_epic(instrument_upper):
            # Double-check: if it's a known epic value, return it
            if instrument_upper in cls.EPIC_MAP.values():
                logger.debug(f"Using {instrument_upper} as-is (valid epic)")
                return instrument_upper
            # If it's a valid epic format but not in our map, return it
            logger.debug(f"Using {instrument_upper} as-is (valid epic format)")
            return instrument_upper

        # Try to construct epic from instrument name
        # Format: EUR_USD -> EURUSD or EURUSD -> EURUSD (passthrough)
        try:
            # Handle format with underscore: EUR_USD
            if '_' in instrument_upper:
                parts = instrument_upper.split('_')
                base = parts[0]
                quote = parts[1] if len(parts) > 1 else ''
                
                # For forex pairs (3-char base + 3-char quote), construct 6-character epic
                if len(base) == 3 and len(quote) == 3:
                    epic = f"{base}{quote}"
                    logger.info(f"Mapped {instrument} to {epic} (constructed from underscore format)")
                    return epic
                else:
                    # For non-standard pairs, check if we can construct a valid epic
                    # But first, warn that it should be in EPIC_MAP
                    logger.warning(f"Non-standard instrument format: {instrument}. "
                                 f"Expected format like EUR_USD or add to EPIC_MAP.")
                    # Try to return as-is if it might be valid
                    return instrument_upper

            # Handle format without underscore: EURUSD (6 characters) - already correct format
            elif len(instrument_upper) == 6 and instrument_upper.isalpha():
                logger.debug(f"Using {instrument_upper} as-is (already in epic format)")
                return instrument_upper

            else:
                logger.warning(f"Could not parse instrument format: {instrument} "
                             f"(expected EUR_USD, EURUSD, or add to EPIC_MAP)")
                return instrument_upper

        except Exception as e:
            logger.warning(f"Could not parse instrument format: {instrument} - {e}")
            return instrument_upper
    
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

        Capital.com uses simple, human-readable formats (NOT dot-separated like IG Markets):
        - Forex: 6-char alphabetic (EURUSD, GBPJPY)
        - Crypto: 6-7 char alphabetic (BTCUSD, DOGEUSD)
        - Indices: alphanumeric (US500, UK100, DE40, J225, HK50, VIX, RTY)
        - Commodities: alphabetic with optional underscore (GOLD, OIL_CRUDE)
        - Stocks: 2-5 char alphabetic (AAPL, TSLA, META)

        NOTE: This validates epic FORMATS, not instrument names.
        Instrument names like 'XAU_USD' should be mapped via EPIC_MAP first.
        """
        if not epic or not isinstance(epic, str):
            return False

        epic_upper = epic.upper().strip()

        # Check if it's a known epic VALUE in our mapping (actual epics like 'GOLD', 'EURUSD')
        if epic_upper in cls.EPIC_MAP.values():
            return True

        # IMPORTANT: If it's a KEY in EPIC_MAP (like 'XAU_USD'), it's an instrument name, not an epic
        # Don't validate it as an epic - it needs to be mapped first
        if epic_upper in cls.EPIC_MAP:
            return False

        # Forex pairs: exactly 6 alphabetic uppercase characters
        if len(epic_upper) == 6 and epic_upper.isalpha() and epic_upper.isupper():
            return True

        # Crypto pairs: 6-7 alphabetic uppercase characters (BTCUSD, DOGEUSD)
        if 6 <= len(epic_upper) <= 7 and epic_upper.isalpha() and epic_upper.isupper():
            return True

        # Indices: alphanumeric uppercase, 2-5 chars (US500, UK100, DE40, VIX, RTY)
        if 2 <= len(epic_upper) <= 5 and epic_upper.isalnum() and epic_upper.isupper():
            return True

        # Commodities with underscore: uppercase alphanumeric with single underscore
        # BUT: Only if it's a known epic value (like 'OIL_CRUDE'), not an instrument name
        if '_' in epic_upper:
            # Check if it's a known epic value
            if epic_upper in cls.EPIC_MAP.values():
                return True
            # Otherwise, it might be an instrument name (like 'XAU_USD'), not a valid epic
            # Only accept if it matches the pattern and is not in EPIC_MAP as a key
            if epic_upper.replace('_', '').isalnum() and epic_upper.isupper():
                # This could be a valid epic format, but be cautious
                # Prefer checking EPIC_MAP first in to_capitalcom_epic()
                return True

        # Stock tickers: 2-5 alphabetic uppercase characters
        if 2 <= len(epic_upper) <= 5 and epic_upper.isalpha() and epic_upper.isupper():
            return True

        return False
    
    @classmethod
    def add_mapping(cls, instrument: str, epic: str) -> None:
        """Add a custom instrument to epic mapping"""
        cls.EPIC_MAP[instrument] = epic
        cls._REVERSE_MAP = None  # Reset reverse map to force regeneration

