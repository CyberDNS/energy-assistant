# syntax=docker/dockerfile:1
# ── Nornwatt — production container image ───────────────────────────
# Multi-stage build: build the web UI with Node, compile/install the Python
# dependencies in a builder stage, then copy only the installed package into a
# minimal runtime image.
# This keeps the final image lean and avoids shipping build tooling.

# Versions of the base images (global: declared before the first FROM)
ARG PYTHON_VERSION=3.14
ARG NODE_VERSION=24

# --------------------------------------------------------------------------
# Stage 1 — frontend: build the web UI (Vite) into the Python package
# --------------------------------------------------------------------------
# Runs on the build machine's own architecture: the result is plain JS/CSS,
# so there is no need to emulate the target platform for it.
FROM --platform=$BUILDPLATFORM node:${NODE_VERSION}-slim AS frontend

WORKDIR /build/frontend

# Dependencies first, so source changes don't invalidate the install layer
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
# Writes to /build/src/nornwatt/frontend (see frontend/vite.config.ts)
RUN npm run build

# --------------------------------------------------------------------------
# Stage 2 — builder: install all Python dependencies and the package
# --------------------------------------------------------------------------
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
COPY --from=frontend /build/src/nornwatt/frontend ./src/nornwatt/frontend

# Install the package and its runtime dependencies into an isolated prefix
RUN uv pip install --system --no-cache .

# --------------------------------------------------------------------------
# Stage 3 — runtime: minimal image with only what is needed to run
# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="Nornwatt"
LABEL org.opencontainers.image.description="Open-source, vendor-neutral energy management platform"
LABEL org.opencontainers.image.source="https://github.com/CyberDNS/nornwatt"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# Create a non-root user for security
RUN groupadd --gid 1000 nornwatt \
    && useradd --uid 1000 --gid nornwatt --no-create-home nornwatt

WORKDIR /app

# Copy installed packages from builder.
# Copying /usr/local/lib captures all Python site-packages regardless of the
# exact Python micro-version directory name.
COPY --from=builder /usr/local/lib /usr/local/lib
COPY --from=builder /usr/local/bin/nornwatt /usr/local/bin/nornwatt

# Runtime data and config directories
# Config is expected to be mounted at /config/config.yaml
# Data (SQLite DB) is persisted at /data
RUN mkdir -p /config /data && chown nornwatt:nornwatt /config /data

USER nornwatt

# Default environment
ENV LOG_LEVEL=INFO

# Build identity, shown in the UI as e.g. 0.1.3-dev+1a2b3c4 (see version.py).
# Set by docker-publish.yml; declared last so a new commit doesn't invalidate
# the cached layers above.
ARG GIT_COMMIT=""
ARG BUILD_CHANNEL=""
ENV NORNWATT_COMMIT=${GIT_COMMIT} \
    NORNWATT_CHANNEL=${BUILD_CHANNEL}

# API port (default in application is 8088; override via config.yaml server.port)
EXPOSE 8088

# Health check — poll the API root endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8088/health')" || exit 1

# Entrypoint: pass config path and db path from well-known container locations
ENTRYPOINT ["nornwatt", "/config/config.yaml", "--db", "/data/history.db"]
