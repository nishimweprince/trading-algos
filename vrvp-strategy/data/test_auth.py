"""Standalone authentication testing utility"""
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from loguru import logger
from config import load_config
from data.capitalcom_client import CapitalComAPIClient

def test_authentication():
    """Test Capital.com authentication with detailed logging"""
    logger.info("=" * 60)
    logger.info("Capital.com Authentication Test")
    logger.info("=" * 60)

    config = load_config()

    # Validate config
    issues = config.capitalcom.validate()
    if issues:
        logger.error("Configuration issues found:")
        for issue in issues:
            logger.error(f"  - {issue}")
        logger.error("\nPlease fix these issues in your .env file")
        return False

    logger.info(f"Environment: {config.capitalcom.environment}")
    logger.info(f"API URL: {config.capitalcom.api_url}")
    logger.info(f"API Key: {config.capitalcom.api_key[:8]}...")
    logger.info(f"Username: {config.capitalcom.username or '(not set)'}")

    # Test authentication
    try:
        client = CapitalComAPIClient(
            config.capitalcom.api_key,
            config.capitalcom.api_password,
            config.capitalcom.username,
            config.capitalcom.environment
        )

        logger.info("\nAttempting authentication...")
        tokens = client.authenticate()

        logger.info("=" * 60)
        logger.info("SUCCESS: Authentication successful!")
        logger.info(f"CST: {tokens['cst'][:20]}...")
        logger.info(f"Security Token: {tokens['security_token'][:20]}...")
        logger.info("=" * 60)

        # Test API call
        logger.info("\nTesting API call: get_accounts()")
        accounts = client.get_accounts()
        logger.info(f"Accounts response: {accounts}")

        logger.info("\nTest complete!")
        return True

    except Exception as e:
        logger.error("=" * 60)
        logger.error(f"FAILED: Authentication failed")
        logger.error(f"Error: {e}")
        logger.error("=" * 60)
        logger.error("\nTroubleshooting steps:")
        logger.error("1. Verify your credentials at https://capital.com/trading/platform/")
        logger.error("2. Check if API key is enabled in Capital.com dashboard")
        logger.error("3. Ensure you're using DEMO environment credentials")
        logger.error("4. Try regenerating your API key")
        return False

if __name__ == '__main__':
    from monitoring import setup_logging, LoggingConfig

    # Enable debug logging for testing
    log_config = LoggingConfig(level='DEBUG')
    setup_logging(log_config)

    success = test_authentication()
    sys.exit(0 if success else 1)
