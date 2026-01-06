"""Instrument name mapping utility for converting between formats"""
from typing import Dict, Optional
from loguru import logger


class InstrumentMapper:
    """Utility for instrument name conversion between different formats"""
    
    # Mapping from standard format (EUR_USD) to Capital.com epics
    EPIC_MAP: Dict[str, str] = {
        'EUR_USD': 'CS.D.EURUSD.CFD.IP',
        'GBP_USD': 'CS.D.GBPUSD.CFD.IP',
        'USD_JPY': 'CS.D.USDJPY.CFD.IP',
        'USD_CHF': 'CS.D.USDCHF.CFD.IP',
        'AUD_USD': 'CS.D.AUDUSD.CFD.IP',
        'USD_CAD': 'CS.D.USDCAD.CFD.IP',
        'NZD_USD': 'CS.D.NZDUSD.CFD.IP',
        'EUR_GBP': 'CS.D.EURGBP.CFD.IP',
        'EUR_JPY': 'CS.D.EURJPY.CFD.IP',
        'GBP_JPY': 'CS.D.GBPJPY.CFD.IP',
        'AUD_JPY': 'CS.D.AUDJPY.CFD.IP',
        'EUR_CHF': 'CS.D.EURCHF.CFD.IP',
        'AUD_NZD': 'CS.D.AUDNZD.CFD.IP',
        'EUR_AUD': 'CS.D.EURAUD.CFD.IP',
        'GBP_AUD': 'CS.D.GBPAUD.CFD.IP',
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
        """Convert standard format (EUR_USD) to Capital.com epic (CS.D.EURUSD.CFD.IP)"""
        # Check if already an epic
        if cls.is_valid_epic(instrument):
            return instrument
        
        # Try direct mapping
        if instrument in cls.EPIC_MAP:
            return cls.EPIC_MAP[instrument]
        
        # Try to construct epic from instrument name
        # Format: EUR_USD -> CS.D.EURUSD.CFD.IP
        try:
            base, quote = instrument.split('_')
            epic = f"CS.D.{base}{quote}.CFD.IP"
            logger.info(f"Mapped {instrument} to {epic} (constructed)")
            return epic
        except ValueError:
            logger.warning(f"Could not parse instrument format: {instrument}")
            return instrument
    
    @classmethod
    def from_capitalcom_epic(cls, epic: str) -> str:
        """Convert Capital.com epic to standard format"""
        # Check reverse mapping
        reverse_map = cls._get_reverse_map()
        if epic in reverse_map:
            return reverse_map[epic]
        
        # Try to parse epic format: CS.D.EURUSD.CFD.IP -> EUR_USD
        try:
            if epic.startswith('CS.D.') and epic.endswith('.CFD.IP'):
                # Extract currency pair
                pair = epic[5:-7]  # Remove 'CS.D.' and '.CFD.IP'
                # Split into base and quote (assuming 3-letter codes)
                if len(pair) == 6:
                    base = pair[:3]
                    quote = pair[3:]
                    standard = f"{base}_{quote}"
                    logger.info(f"Mapped {epic} to {standard} (parsed)")
                    return standard
        except Exception as e:
            logger.warning(f"Could not parse epic format: {epic} - {e}")
        
        return epic
    
    @classmethod
    def is_valid_epic(cls, epic: str) -> bool:
        """Validate if string is a valid Capital.com epic format"""
        # Capital.com epics typically follow pattern: CS.D.XXXXXX.CFD.IP
        return epic.startswith('CS.D.') and epic.endswith('.CFD.IP') and len(epic) > 10
    
    @classmethod
    def add_mapping(cls, instrument: str, epic: str) -> None:
        """Add a custom instrument to epic mapping"""
        cls.EPIC_MAP[instrument] = epic
        cls._REVERSE_MAP = None  # Reset reverse map to force regeneration

