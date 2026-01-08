#!/usr/bin/env python3
"""
VRVP Strategy Server - FastAPI with Uvicorn

Run the trading strategy as a background service with REST API.

Usage:
    python server.py                    # Default: host=0.0.0.0, port=8000
    python server.py --port 8080        # Custom port
    python server.py --host 127.0.0.1   # Localhost only
    python server.py --reload           # Development mode with auto-reload

Environment Variables:
    INSTRUMENTS=EUR_USD,GBP_USD,USD_JPY   # Comma-separated trading pairs
    CAPITALCOM_API_KEY=...                # Capital.com API key
    CAPITALCOM_API_PASSWORD=...           # Capital.com API password
    CAPITALCOM_USERNAME=...               # Capital.com username
    CAPITALCOM_ENVIRONMENT=demo           # 'demo' or 'live'
    FETCH_INTERVAL_MINUTES=5              # Data fetch interval
    TIMEFRAME=1H                          # Lower timeframe
    LOG_LEVEL=INFO                        # Logging level
"""
import sys
import os
import argparse
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.absolute()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

os.chdir(project_root)


def main():
    parser = argparse.ArgumentParser(
        description='VRVP Strategy Server - FastAPI with Uvicorn'
    )
    parser.add_argument(
        '--host',
        type=str,
        default=os.getenv('HOST', '0.0.0.0'),
        help='Host to bind to (default: 0.0.0.0)'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=int(os.getenv('PORT', '8000')),
        help='Port to bind to (default: 8000)'
    )
    parser.add_argument(
        '--reload',
        action='store_true',
        help='Enable auto-reload for development'
    )
    parser.add_argument(
        '--workers',
        type=int,
        default=1,
        help='Number of worker processes (default: 1, recommended for trading)'
    )
    parser.add_argument(
        '--log-level',
        type=str,
        default=os.getenv('LOG_LEVEL', 'info').lower(),
        choices=['debug', 'info', 'warning', 'error', 'critical'],
        help='Log level (default: info)'
    )

    args = parser.parse_args()

    # Import uvicorn here to avoid loading it when just showing help
    import uvicorn

    print("=" * 60)
    print("VRVP Strategy Server")
    print("=" * 60)
    print(f"Host: {args.host}")
    print(f"Port: {args.port}")
    print(f"Workers: {args.workers}")
    print(f"Log Level: {args.log_level}")
    print(f"Reload: {args.reload}")
    print("=" * 60)
    print(f"\nAPI Documentation: http://{args.host}:{args.port}/docs")
    print(f"Health Check: http://{args.host}:{args.port}/health")
    print(f"Status: http://{args.host}:{args.port}/status")
    print("\n" + "=" * 60)

    # Run uvicorn
    uvicorn.run(
        "api.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers if not args.reload else 1,
        log_level=args.log_level
    )


if __name__ == '__main__':
    main()
