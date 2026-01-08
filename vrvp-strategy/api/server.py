"""FastAPI server for VRVP Strategy"""
import sys
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

# Add project root to path for imports
project_root = Path(__file__).parent.parent.absolute()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from config import load_config
from monitoring import setup_logging

from .models import (
    HealthResponse, HealthStatus,
    SignalResponse, SignalTypeEnum,
    PairStatusResponse, PairStatus,
    StrategyStatusResponse,
    ErrorResponse
)
from .strategy_runner import StrategyRunner

# Global strategy runner instance
_strategy_runner: Optional[StrategyRunner] = None


def get_strategy_runner() -> StrategyRunner:
    """Get the global strategy runner instance"""
    global _strategy_runner
    if _strategy_runner is None:
        raise RuntimeError("Strategy runner not initialized")
    return _strategy_runner


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown"""
    global _strategy_runner

    # Startup
    logger.info("Starting VRVP Strategy API Server...")

    # Load config and setup logging
    config = load_config()
    setup_logging(config.logging)

    # Log configuration
    logger.info(f"Environment: {config.capitalcom.environment}")
    logger.info(f"Instruments: {config.trading.instruments}")
    logger.info(f"Timeframes: LTF={config.trading.timeframe}, HTF={config.trading.htf_timeframe}")
    logger.info(f"Fetch interval: {config.trading.fetch_interval_minutes} minutes")

    # Initialize and start strategy runner
    _strategy_runner = StrategyRunner(config)

    if _strategy_runner.start():
        logger.info("Strategy runner started successfully")
    else:
        logger.error("Failed to start strategy runner")
        # Don't fail startup - let health endpoint report unhealthy status

    yield

    # Shutdown
    logger.info("Shutting down VRVP Strategy API Server...")
    if _strategy_runner:
        _strategy_runner.stop()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application"""
    app = FastAPI(
        title="VRVP Strategy API",
        description="FastAPI server for running VRVP trading strategy on multiple pairs",
        version="1.0.0",
        lifespan=lifespan
    )

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    return app


# Create the app instance
app = create_app()


@app.get("/", response_model=dict)
async def root():
    """Root endpoint - basic info"""
    return {
        "name": "VRVP Strategy API",
        "version": "1.0.0",
        "docs": "/docs"
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint"""
    try:
        runner = get_strategy_runner()
        is_healthy = runner.is_healthy()
        running_count = runner.get_running_pairs_count()
        total_count = len(runner.pairs)

        if is_healthy and running_count == total_count:
            status = HealthStatus.HEALTHY
        elif running_count > 0:
            status = HealthStatus.DEGRADED
        else:
            status = HealthStatus.UNHEALTHY

        return HealthResponse(
            status=status,
            timestamp=datetime.now(),
            pairs_running=running_count,
            pairs_total=total_count,
            authenticated=runner.authenticated
        )
    except RuntimeError:
        return HealthResponse(
            status=HealthStatus.UNHEALTHY,
            timestamp=datetime.now(),
            pairs_running=0,
            pairs_total=0,
            authenticated=False
        )


@app.get("/status", response_model=StrategyStatusResponse)
async def get_status():
    """Get overall strategy status"""
    try:
        runner = get_strategy_runner()
        config = runner.config

        pairs_status = []
        for instrument, pair in runner.pairs.items():
            last_signal = None
            if pair.last_signal:
                signal = pair.last_signal
                signal_type = SignalTypeEnum(signal.type.name)
                last_signal = SignalResponse(
                    instrument=instrument,
                    signal_type=signal_type,
                    price=signal.price,
                    strength=signal.strength,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                    reasons=signal.reasons,
                    timestamp=signal.timestamp or datetime.now()
                )

            pairs_status.append(PairStatusResponse(
                instrument=instrument,
                status=PairStatus(pair.status),
                last_signal=last_signal,
                last_update=pair.last_update,
                error_message=pair.error_message,
                candles_ltf=pair.candles_ltf,
                candles_htf=pair.candles_htf
            ))

        return StrategyStatusResponse(
            status="running" if runner.running else "stopped",
            running=runner.running,
            authenticated=runner.authenticated,
            environment=config.capitalcom.environment,
            pairs=pairs_status,
            fetch_interval_minutes=config.trading.fetch_interval_minutes,
            timeframe=config.trading.timeframe,
            htf_timeframe=config.trading.htf_timeframe,
            started_at=runner.started_at
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/pairs", response_model=List[PairStatusResponse])
async def get_all_pairs():
    """Get status of all trading pairs"""
    try:
        runner = get_strategy_runner()
        result = []

        for instrument, pair in runner.pairs.items():
            last_signal = None
            if pair.last_signal:
                signal = pair.last_signal
                signal_type = SignalTypeEnum(signal.type.name)
                last_signal = SignalResponse(
                    instrument=instrument,
                    signal_type=signal_type,
                    price=signal.price,
                    strength=signal.strength,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                    reasons=signal.reasons,
                    timestamp=signal.timestamp or datetime.now()
                )

            result.append(PairStatusResponse(
                instrument=instrument,
                status=PairStatus(pair.status),
                last_signal=last_signal,
                last_update=pair.last_update,
                error_message=pair.error_message,
                candles_ltf=pair.candles_ltf,
                candles_htf=pair.candles_htf
            ))

        return result
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/pairs/{instrument}", response_model=PairStatusResponse)
async def get_pair_status(instrument: str):
    """Get status of a specific trading pair"""
    try:
        runner = get_strategy_runner()
        pair = runner.get_pair_status(instrument)

        if not pair:
            raise HTTPException(status_code=404, detail=f"Pair {instrument} not found")

        last_signal = None
        if pair.last_signal:
            signal = pair.last_signal
            signal_type = SignalTypeEnum(signal.type.name)
            last_signal = SignalResponse(
                instrument=instrument,
                signal_type=signal_type,
                price=signal.price,
                strength=signal.strength,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                reasons=signal.reasons,
                timestamp=signal.timestamp or datetime.now()
            )

        return PairStatusResponse(
            instrument=instrument,
            status=PairStatus(pair.status),
            last_signal=last_signal,
            last_update=pair.last_update,
            error_message=pair.error_message,
            candles_ltf=pair.candles_ltf,
            candles_htf=pair.candles_htf
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/signals", response_model=List[SignalResponse])
async def get_all_signals():
    """Get latest signals for all pairs"""
    try:
        runner = get_strategy_runner()
        result = []

        for instrument, pair in runner.pairs.items():
            if pair.last_signal:
                signal = pair.last_signal
                signal_type = SignalTypeEnum(signal.type.name)
                result.append(SignalResponse(
                    instrument=instrument,
                    signal_type=signal_type,
                    price=signal.price,
                    strength=signal.strength,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                    reasons=signal.reasons,
                    timestamp=signal.timestamp or datetime.now()
                ))

        return result
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/signals/{instrument}", response_model=SignalResponse)
async def get_pair_signal(instrument: str):
    """Get latest signal for a specific pair"""
    try:
        runner = get_strategy_runner()
        signal = runner.get_latest_signal(instrument)

        if not signal:
            raise HTTPException(
                status_code=404,
                detail=f"No signal available for {instrument}"
            )

        signal_type = SignalTypeEnum(signal.type.name)
        return SignalResponse(
            instrument=instrument,
            signal_type=signal_type,
            price=signal.price,
            strength=signal.strength,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            reasons=signal.reasons,
            timestamp=signal.timestamp or datetime.now()
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/restart", response_model=dict)
async def restart_strategy():
    """Restart the strategy runner"""
    try:
        runner = get_strategy_runner()

        logger.info("Restarting strategy runner...")
        runner.stop()

        if runner.start():
            return {"status": "restarted", "message": "Strategy runner restarted successfully"}
        else:
            raise HTTPException(
                status_code=500,
                detail="Failed to restart strategy runner"
            )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/stop", response_model=dict)
async def stop_strategy():
    """Stop the strategy runner"""
    try:
        runner = get_strategy_runner()
        runner.stop()
        return {"status": "stopped", "message": "Strategy runner stopped"}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/start", response_model=dict)
async def start_strategy():
    """Start the strategy runner"""
    try:
        runner = get_strategy_runner()

        if runner.running:
            return {"status": "already_running", "message": "Strategy runner is already running"}

        if runner.start():
            return {"status": "started", "message": "Strategy runner started successfully"}
        else:
            raise HTTPException(
                status_code=500,
                detail="Failed to start strategy runner"
            )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
