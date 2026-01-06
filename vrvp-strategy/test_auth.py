#!/usr/bin/env python3
"""
Standalone Capital.com Authentication Test Utility

This script helps diagnose authentication issues by testing your credentials
in isolation with detailed error messages.
"""
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from loguru import logger
from config import load_config
from data.capitalcom_client import CapitalComAPIClient

def test_authentication():
    """Test Capital.com authentication with detailed logging"""
    logger.info("=" * 70)
    logger.info("Capital.com Authentication Diagnostic Test")
    logger.info("=" * 70)

    config = load_config()

    # Validate config
    issues = config.capitalcom.validate()
    if issues:
        logger.error("\n❌ Configuration issues found:")
        for issue in issues:
            logger.error(f"   • {issue}")
        logger.error("\nPlease fix these issues in your .env file")
        return False

    logger.info(f"\n✓ Configuration loaded successfully")
    logger.info(f"  Environment: {config.capitalcom.environment}")
    logger.info(f"  API URL: {config.capitalcom.api_url}")
    logger.info(f"  API Key: {'*' * (len(config.capitalcom.api_key) - 4) + config.capitalcom.api_key[-4:] if len(config.capitalcom.api_key) > 4 else '***'}")
    logger.info(f"  Username: {config.capitalcom.username or '(NOT SET - this may be the issue!)'}")
    logger.info(f"  Password: {'*' * len(config.capitalcom.api_password) if config.capitalcom.api_password else '(NOT SET)'}")

    if not config.capitalcom.username:
        logger.warning("\n⚠️  WARNING: CAPITALCOM_USERNAME is not set!")
        logger.warning("   Capital.com typically requires your account username/email as the identifier.")
        logger.warning("   The API key alone may not work for authentication.")

    # Test authentication
    try:
        logger.info("\n" + "=" * 70)
        logger.info("Attempting authentication...")
        logger.info("=" * 70)

        client = CapitalComAPIClient(
            config.capitalcom.api_key,
            config.capitalcom.api_password,
            config.capitalcom.username,
            config.capitalcom.environment
        )

        tokens = client.authenticate()

        logger.info("\n" + "=" * 70)
        logger.info("✅ SUCCESS: Authentication successful!")
        logger.info("=" * 70)
        logger.info(f"  CST Token: {tokens['cst'][:30]}...")
        logger.info(f"  Security Token: {tokens['security_token'][:30]}...")
        logger.info("=" * 70)

        # Test API call
        logger.info("\nTesting API call: get_accounts()...")
        accounts = client.get_accounts()
        logger.info(f"✓ API call successful!")
        logger.info(f"  Response keys: {list(accounts.keys())}")
        if 'accounts' in accounts and len(accounts['accounts']) > 0:
            acc = accounts['accounts'][0]
            logger.info(f"  Account balance: {acc.get('balance', 'N/A')}")
            logger.info(f"  Account currency: {acc.get('currency', 'N/A')}")

        logger.info("\n✅ All tests passed! Your credentials are working correctly.")
        return True

    except Exception as e:
        logger.error("\n" + "=" * 70)
        logger.error("❌ FAILED: Authentication failed")
        logger.error("=" * 70)
        logger.error(f"Error: {str(e)}")
        logger.error("=" * 70)
        
        logger.error("\n📋 Troubleshooting Checklist:")
        logger.error("  1. ✓ Verify CAPITALCOM_USERNAME is your Capital.com login username/email")
        logger.error("  2. ✓ Verify CAPITALCOM_API_PASSWORD is the password you set when creating the API key")
        logger.error("  3. ✓ Verify CAPITALCOM_API_KEY matches the key in Capital.com dashboard")
        logger.error("  4. ✓ Ensure you're using DEMO credentials for demo environment")
        logger.error("  5. ✓ Check if API key is enabled in Capital.com Settings > API integrations")
        logger.error("  6. ✓ Try regenerating your API key if credentials are correct")
        logger.error("  7. ✓ Ensure 2FA is enabled on your Capital.com account")
        
        logger.error("\n💡 Common Issues:")
        logger.error("  • Using account password instead of API password")
        logger.error("  • Using API key as username (should be your login email/username)")
        logger.error("  • API key not enabled in dashboard")
        logger.error("  • Mixing demo and live credentials")
        
        return False

if __name__ == '__main__':
    from monitoring import setup_logging
    from config import LoggingConfig
    
    # Setup logging
    logging_config = LoggingConfig(level='INFO')
    setup_logging(logging_config)
    
    success = test_authentication()
    sys.exit(0 if success else 1)

