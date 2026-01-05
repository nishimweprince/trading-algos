#!/usr/bin/env python3
"""
Test Massive API authentication and basic connectivity
"""
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.absolute()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import os
from dotenv import load_dotenv
from loguru import logger

from config import load_config
from data.massive_client import MassiveAPIClient

def test_authentication():
    """Test Massive API key authentication"""
    logger.info("=" * 60)
    logger.info("Testing Massive API Authentication")
    logger.info("=" * 60)
    
    # Load config
    config = load_config()
    
    # Check if API key is set
    if not config.massive.api_key:
        logger.error("MASSIVE_API_KEY is not set in .env file")
        logger.error("Please set it: MASSIVE_API_KEY=your_api_key_here")
        return False
    
    logger.info(f"API Key: {config.massive.api_key[:10]}...{config.massive.api_key[-4:]}")
    logger.info(f"Base URL: {config.massive.base_url}")
    logger.info(f"Rate Limit: {config.massive.rate_limit_per_minute} req/min")
    
    # Create client
    try:
        client = MassiveAPIClient(
            api_key=config.massive.api_key,
            rate_limit_per_minute=config.massive.rate_limit_per_minute,
            base_url=config.massive.base_url
        )
        logger.info("✓ Client created successfully")
    except Exception as e:
        logger.error(f"✗ Failed to create client: {e}")
        return False
    
    # Test a simple API call (get last quote for EUR/USD)
    try:
        logger.info("\nTesting API call: get_last_quote('EUR', 'USD')")
        response = client.get_last_quote('EUR', 'USD')
        
        if response.get('status') == 'OK':
            last = response.get('last', {})
            bid = last.get('bid', 0)
            ask = last.get('ask', 0)
            logger.info(f"✓ Authentication successful!")
            logger.info(f"  EUR/USD: Bid={bid}, Ask={ask}, Spread={ask-bid}")
            return True
        else:
            logger.error(f"✗ API returned error: {response.get('error', 'Unknown error')}")
            return False
            
    except Exception as e:
        logger.error(f"✗ API call failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

if __name__ == '__main__':
    success = test_authentication()
    sys.exit(0 if success else 1)

