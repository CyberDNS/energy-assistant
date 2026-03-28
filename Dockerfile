# syntax=docker/dockerfile:1
# ── Energy Assistant — production container image ───────────────────────────
# Multi-stage build: compile/install dependencies in a builder stage,
# then copy only the installed package into a minimal runtime image.
# This keeps the final image lean and avoids shipping build tooling.

# --------------------------------------------------------------------------
# Stage 1 — builder: install all Python dependencies and the package
# --------------------------------------------------------------------------
ARG PYTHON_VERSION=3.14

FROM python:${PYTHON_VERSION}-slim AS builder

WORKDIR /build

# Install native build toolchain needed to compile Python packages from source.
# Required on ARM (linux/arm64, linux/arm/v7) where pre-built wheels may be
# unavailable (e.g. numpy pulled in transitively by highspy).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gfortran \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency installation
RUN pip install --no-cache-dir uv

# Copy only the files needed to resolve and install dependencies first
# (maximises Docker layer cache — source changes don't invalidate deps)
COPY pyproject.toml uv.lock* README.md ./
COPY src/ ./src/

# Install the package and its runtime dependencies into an isolated prefix
RUN uv pip install --system --no-cache .

# --------------------------------------------------------------------------
# Stage 2 — runtime: minimal image with only what is needed to run
# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="Energy Assistant"
LABEL org.opencontainers.image.description="Open-source, vendor-neutral energy management platform"
LABEL org.opencontainers.image.source="https://github.com/CyberDNS/energy-assistant"
LABEL org.opencontainers.image.licenses="MIT"

# Create a non-root user for security
RUN groupadd --gid 1000 energyassistant \
    && useradd --uid 1000 --gid energyassistant --no-create-home energyassistant

WORKDIR /app

# Copy installed packages from builder.
# Copying /usr/local/lib captures all Python site-packages regardless of the
# exact Python micro-version directory name.
COPY --from=builder /usr/local/lib /usr/local/lib
COPY --from=builder /usr/local/bin/energy-assistant /usr/local/bin/energy-assistant

# Runtime data and config directories
# Config is expected to be mounted at /config/config.yaml
# Data (SQLite DB) is persisted at /data
RUN mkdir -p /config /data && chown energyassistant:energyassistant /config /data

USER energyassistant

# Default environment
ENV LOG_LEVEL=INFO

# API port (default in application is 8088; override via config.yaml server.port)
EXPOSE 8088

# Health check — poll the API root endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8088/health')" || exit 1

# Entrypoint: pass config path and db path from well-known container locations
ENTRYPOINT ["energy-assistant", "/config/config.yaml", "--db", "/data/history.db"]
