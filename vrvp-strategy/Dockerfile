# VRVP Strategy - Docker Image
# Multi-stage build for efficient production deployment

# =============================================================================
# Stage 1: Builder - Install dependencies
# =============================================================================
FROM python:3.12-slim as builder

# Set environment variables for Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install build dependencies with retry logic for network issues
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get clean || true && \
    (apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        libffi-dev \
    || (sleep 5 && apt-get clean && apt-get update && apt-get install -y --no-install-recommends gcc g++ libffi-dev) \
    || (sleep 10 && apt-get clean && apt-get update && apt-get install -y --no-install-recommends gcc g++ libffi-dev)) && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements file and install script
COPY requirements-main.txt .
COPY install.sh .

# Create virtual environment and install dependencies
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Make install script executable and run it
RUN chmod +x install.sh && \
    sh install.sh --upgrade

# =============================================================================
# Stage 2: Production - Final lightweight image
# =============================================================================
FROM python:3.12-slim as production

# Labels
LABEL maintainer="VRVP Strategy" \
    description="Volume Profile Trading Strategy with FastAPI" \
    version="1.0"

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONPATH=/app \
    # Application defaults
    HOST=0.0.0.0 \
    PORT=8000 \
    LOG_LEVEL=INFO \
    # Timezone (can be overridden)
    TZ=UTC

# Create non-root user for security
RUN groupadd --gid 1000 trading && \
    useradd --uid 1000 --gid trading --shell /bin/bash --create-home trading

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Set working directory
WORKDIR /app

# Copy application code
COPY --chown=trading:trading . .

# Create logs directory
RUN mkdir -p /app/logs && chown -R trading:trading /app/logs

# Switch to non-root user
USER trading

# Expose the API port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:${PORT}/health', timeout=5).raise_for_status()" || exit 1

# Default command - run the FastAPI server
CMD ["python", "server.py"]
