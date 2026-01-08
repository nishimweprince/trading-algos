#!/bin/bash
#
# VRVP Strategy - Installation Script
#
# This script installs all dependencies for the VRVP trading strategy.
# It handles smartmoneyconcepts specially (--no-deps) due to outdated dependencies.
#
# Usage:
#   ./install.sh              # Install in current environment
#   ./install.sh --venv       # Create and use virtual environment
#   ./install.sh --help       # Show help
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Print colored output
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Show help
show_help() {
    echo "VRVP Strategy - Installation Script"
    echo ""
    echo "Usage: ./install.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --venv          Create and activate a virtual environment"
    echo "  --upgrade       Upgrade pip before installing"
    echo "  --help          Show this help message"
    echo ""
    echo "Examples:"
    echo "  ./install.sh                    # Install in current environment"
    echo "  ./install.sh --venv             # Create venv and install"
    echo "  ./install.sh --venv --upgrade   # Create venv, upgrade pip, install"
}

# Parse arguments
USE_VENV=false
UPGRADE_PIP=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --venv)
            USE_VENV=true
            shift
            ;;
        --upgrade)
            UPGRADE_PIP=true
            shift
            ;;
        --help)
            show_help
            exit 0
            ;;
        *)
            print_error "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
done

# Change to script directory
cd "$SCRIPT_DIR"

echo "=============================================="
echo "  VRVP Strategy - Installation"
echo "=============================================="
echo ""

# Create virtual environment if requested
if [ "$USE_VENV" = true ]; then
    if [ -d "venv" ]; then
        print_warn "Virtual environment already exists"
    else
        print_info "Creating virtual environment..."
        python3 -m venv venv
    fi
    
    print_info "Activating virtual environment..."
    source venv/bin/activate
    print_info "Using Python: $(which python)"
fi

# Upgrade pip if requested
if [ "$UPGRADE_PIP" = true ]; then
    print_info "Upgrading pip..."
    pip install --upgrade pip
fi

# Step 1: Install smartmoneyconcepts with --no-deps
print_info "Step 1/2: Installing smartmoneyconcepts (with --no-deps)..."
pip install smartmoneyconcepts==0.0.26 --no-deps

# Step 2: Install remaining requirements
print_info "Step 2/2: Installing remaining dependencies..."
pip install -r requirements-main.txt

echo ""
echo "=============================================="
print_info "Installation complete!"
echo "=============================================="
echo ""

# Verify installation
print_info "Verifying installation..."
python -c "
import sys
try:
    import fastapi
    import uvicorn
    import pandas
    import numpy
    import loguru
    import smartmoneyconcepts
    print('✓ All core packages imported successfully')
except ImportError as e:
    print(f'✗ Import error: {e}')
    sys.exit(1)
"

echo ""
print_info "You can now run the strategy:"
echo "  python server.py           # Start FastAPI server"
echo "  python main.py paper -i EUR_USD  # Run paper trading"
echo "  python main.py backtest -i EUR_USD  # Run backtest"
echo ""

if [ "$USE_VENV" = true ]; then
    print_info "To activate the virtual environment in the future:"
    echo "  source venv/bin/activate"
fi
