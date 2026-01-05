#!/usr/bin/env python3
"""
Unit tests for Massive API client and rate limiter
"""
import sys
import time
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.absolute()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import os
from datetime import datetime, timedelta
from loguru import logger

from config import load_config
from data.massive_client import MassiveAPIClient, RateLimiter

def test_rate_limiter():
    """Test rate limiter functionality"""
    logger.info("=" * 60)
    logger.info("Testing Rate Limiter")
    logger.info("=" * 60)
    
    # Create rate limiter: 5 tokens, refill 5 per minute (0.083 per second)
    limiter = RateLimiter(max_tokens=5, refill_rate_per_second=5/60.0)
    
    # Test 1: Acquire 5 tokens immediately
    logger.info("\nTest 1: Acquire 5 tokens immediately")
    acquired = 0
    for i in range(5):
        if limiter.acquire(blocking=False):
            acquired += 1
            logger.info(f"  ✓ Token {i+1} acquired")
        else:
            logger.error(f"  ✗ Token {i+1} NOT acquired")
    
    assert acquired == 5, f"Expected 5 tokens, got {acquired}"
    logger.info("✓ Test 1 passed: All 5 tokens acquired")
    
    # Test 2: 6th token should fail (non-blocking)
    logger.info("\nTest 2: 6th token should fail (non-blocking)")
    if limiter.acquire(blocking=False):
        logger.error("  ✗ 6th token should NOT be available")
        return False
    else:
        logger.info("  ✓ 6th token correctly blocked")
    
    # Test 3: Wait for token refill
    logger.info("\nTest 3: Wait for token refill (should take ~12 seconds for 1 token)")
    start = time.time()
    if limiter.acquire(blocking=True, timeout=15):
        elapsed = time.time() - start
        logger.info(f"  ✓ Token acquired after {elapsed:.2f} seconds")
        logger.info("✓ Test 3 passed: Token refill working")
    else:
        logger.error("  ✗ Token not acquired within timeout")
        return False
    
    return True

def test_api_client():
    """Test Massive API client"""
    logger.info("\n" + "=" * 60)
    logger.info("Testing Massive API Client")
    logger.info("=" * 60)
    
    config = load_config()
    
    if not config.massive.api_key:
        logger.error("MASSIVE_API_KEY not set, skipping API tests")
        return False
    
    client = MassiveAPIClient(
        api_key=config.massive.api_key,
        rate_limit_per_minute=config.massive.rate_limit_per_minute
    )
    
    # Test 1: Check rate limit status
    logger.info("\nTest 1: Check rate limit status")
    status = client.check_rate_limit()
    logger.info(f"  Available tokens: {status['available_tokens']}/{status['max_tokens']}")
    logger.info("✓ Test 1 passed")
    
    # Test 2: Get aggregates (historical candles)
    logger.info("\nTest 2: Get historical aggregates")
    try:
        to_date = datetime.now()
        from_date = to_date - timedelta(days=7)  # Last 7 days
        
        response = client.get_aggregates(
            ticker='C:EURUSD',
            multiplier=1,
            timespan='hour',
            from_date=from_date,
            to_date=to_date
        )
        
        if response.get('status') == 'OK':
            results = response.get('results', [])
            logger.info(f"  ✓ Retrieved {len(results)} candles")
            if results:
                first = results[0]
                logger.info(f"  First candle: O={first.get('o')}, H={first.get('h')}, "
                          f"L={first.get('l')}, C={first.get('c')}, V={first.get('v')}")
            logger.info("✓ Test 2 passed")
        else:
            logger.error(f"  ✗ API error: {response.get('error', 'Unknown')}")
            return False
            
    except Exception as e:
        logger.error(f"  ✗ Failed: {e}")
        return False
    
    # Test 3: Get last quote
    logger.info("\nTest 3: Get last quote")
    try:
        response = client.get_last_quote('EUR', 'USD')
        
        if response.get('status') == 'OK':
            last = response.get('last', {})
            logger.info(f"  ✓ Quote: Bid={last.get('bid')}, Ask={last.get('ask')}")
            logger.info("✓ Test 3 passed")
        else:
            logger.error(f"  ✗ API error: {response.get('error', 'Unknown')}")
            return False
            
    except Exception as e:
        logger.error(f"  ✗ Failed: {e}")
        return False
    
    return True

if __name__ == '__main__':
    logger.info("Running Massive API Client Tests\n")
    
    # Test rate limiter (doesn't require API key)
    rate_limiter_ok = test_rate_limiter()
    
    # Test API client (requires API key)
    api_client_ok = test_api_client()
    
    logger.info("\n" + "=" * 60)
    logger.info("Test Summary")
    logger.info("=" * 60)
    logger.info(f"Rate Limiter: {'✓ PASSED' if rate_limiter_ok else '✗ FAILED'}")
    logger.info(f"API Client: {'✓ PASSED' if api_client_ok else '✗ FAILED'}")
    
    success = rate_limiter_ok and api_client_ok
    sys.exit(0 if success else 1)

