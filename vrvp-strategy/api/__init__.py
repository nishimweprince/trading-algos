"""API module for FastAPI server"""
import sys
from pathlib import Path

# Add project root to path for imports
# This MUST happen before any other imports from this package
project_root = Path(__file__).parent.parent.absolute()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Lazy imports to avoid circular import issues in Docker
# Uvicorn imports api.server:app directly, so we don't need to import here
__all__ = ['app', 'create_app', 'StrategyRunner', 'PairRunner']


def __getattr__(name):
    """Lazy loading of module attributes"""
    if name == 'app':
        from .server import app
        return app
    elif name == 'create_app':
        from .server import create_app
        return create_app
    elif name == 'StrategyRunner':
        from .strategy_runner import StrategyRunner
        return StrategyRunner
    elif name == 'PairRunner':
        from .strategy_runner import PairRunner
        return PairRunner
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
