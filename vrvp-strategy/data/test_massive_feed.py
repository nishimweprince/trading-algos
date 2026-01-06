#!/usr/bin/env python3
"""
Integration tests for Massive data feed
"""
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.absolute()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import os
from datetime import datetime, timedelta
from loguru import logger

from config import load_config
from data.massive_feed import MassiveDataFeed

def test_feed_candles():
    """Test fetching candles from Massive feed"""
    logger.info("=" * 60)
    logger.info("Testing Massive Data Feed - Candles")
    logger.info("=" * 60)
    
    config = load_config()
    
    if not config.massive.api_key:
        logger.error("MASSIVE_API_KEY not set, skipping feed tests")
        return False
    
    feed = MassiveDataFeed(
        api_key=config.massive.api_key,
        rate_limit_per_minute=config.massive.rate_limit_per_minute
    )
    
    # Test 1: Get 1H candles
    logger.info("\nTest 1: Get 1H candles for EUR_USD")
    try:
        df = feed.get_candles('EUR_USD', '1H', count=100)
        
        if len(df) > 0:
            logger.info(f"  ✓ Retrieved {len(df)} candles")
            logger.info(f"  Date range: {df.index[0]} to {df.index[-1]}")
            logger.info(f"  Latest close: {df['close'].iloc[-1]}")
            logger.info("✓ Test 1 passed")
        else:
            logger.error("  ✗ No candles retrieved")
            return False
            
    except Exception as e:
        logger.error(f"  ✗ Failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False
    
    # Test 2: Get 4H candles
    logger.info("\nTest 2: Get 4H candles for EUR_USD")
    try:
        df = feed.get_candles('EUR_USD', '4H', count=50)
        
        if len(df) > 0:
            logger.info(f"  ✓ Retrieved {len(df)} candles")
            logger.info("✓ Test 2 passed")
        else:
            logger.error("  ✗ No candles retrieved")
            return False
            
    except Exception as e:
        logger.error(f"  ✗ Failed: {e}")
        return False
    
    return True

def test_feed_price():
    """Test fetching current price from Massive feed"""
    logger.info("\n" + "=" * 60)
    logger.info("Testing Massive Data Feed - Current Price")
    logger.info("=" * 60)
    
    config = load_config()
    
    if not config.massive.api_key:
        logger.error("MASSIVE_API_KEY not set, skipping price test")
        return False
    
    feed = MassiveDataFeed(
        api_key=config.massive.api_key,
        rate_limit_per_minute=config.massive.rate_limit_per_minute
    )
    
    logger.info("\nTest: Get current price for EUR_USD")
    try:
        price = feed.get_current_price('EUR_USD')
        
        if price['bid'] > 0 and price['ask'] > 0:
            logger.info(f"  ✓ Price: Bid={price['bid']}, Ask={price['ask']}, "
                       f"Mid={price['mid']}, Spread={price['spread']}")
            logger.info("✓ Test passed")
            return True
        else:
            logger.error(f"  ✗ Invalid price data: {price}")
            return False
            
    except Exception as e:
        logger.error(f"  ✗ Failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

def test_feed_multi_timeframe():
    """Test multi-timeframe data fetching"""
    logger.info("\n" + "=" * 60)
    logger.info("Testing Massive Data Feed - Multi-Timeframe")
    logger.info("=" * 60)
    
    config = load_config()
    
    if not config.massive.api_key:
        logger.error("MASSIVE_API_KEY not set, skipping multi-TF test")
        return False
    
    feed = MassiveDataFeed(
        api_key=config.massive.api_key,
        rate_limit_per_minute=config.massive.rate_limit_per_minute
    )
    
    logger.info("\nTest: Get multi-timeframe data (1H and 4H)")
    try:
        data = feed.get_multi_timeframe_data('EUR_USD', '1H', '4H', count=200)
        
        ltf_df = data['current']
        htf_df = data['htf']
        
        if len(ltf_df) > 0 and len(htf_df) > 0:
            logger.info(f"  ✓ LTF (1H): {len(ltf_df)} candles")
            logger.info(f"  ✓ HTF (4H): {len(htf_df)} candles")
            logger.info("✓ Test passed")
            return True
        else:
            logger.error(f"  ✗ Incomplete data: LTF={len(ltf_df)}, HTF={len(htf_df)}")
            return False
            
    except Exception as e:
        logger.error(f"  ✗ Failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

if __name__ == '__main__':
    logger.info("Running Massive Data Feed Integration Tests\n")
    
    candles_ok = test_feed_candles()
    price_ok = test_feed_price()
    multi_tf_ok = test_feed_multi_timeframe()
    
    logger.info("\n" + "=" * 60)
    logger.info("Test Summary")
    logger.info("=" * 60)
    logger.info(f"Candles: {'✓ PASSED' if candles_ok else '✗ FAILED'}")
    logger.info(f"Price: {'✓ PASSED' if price_ok else '✗ FAILED'}")
    logger.info(f"Multi-Timeframe: {'✓ PASSED' if multi_tf_ok else '✗ FAILED'}")
    
    success = candles_ok and price_ok and multi_tf_ok
    sys.exit(0 if success else 1)

