#!/usr/bin/env python3
"""
Wrapper script to run the application with proper package context
"""
import sys
import os
from pathlib import Path

# Get the project root directory
project_root = Path(__file__).parent.absolute()

# Add project root to Python path
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Change to project root directory
os.chdir(project_root)

# Now import and run main
if __name__ == '__main__':
    from main import main
    main()

