# Base image
FROM python:3.13-slim@sha256:2b9c9803c6a287cafa0a8c917211dddd23dcd2016f049690ee5219f5d3f1636e AS base
# UID/GID for non-root user (https://github.com/hexops/dockerfile#do-not-use-a-uid-below-10000)
ARG uid=10001
ARG gid=10001
# Install system dependencies and Tailscale
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl iproute2 iputils-ping gnupg ca-certificates && \
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.gpg | gpg --dearmor -o /usr/share/keyrings/tailscale-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/debian bookworm main" > /etc/apt/sources.list.d/tailscale.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends tailscale && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
# Create non-root user and app directory
RUN groupadd --gid ${gid} outpost && \
    useradd --uid ${uid} --gid ${gid} --create-home outpost
WORKDIR /app
ENV HOME=/home/outpost


# Builder stage - install dependencies with uv
FROM base AS builder
# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.9.29@sha256:db9370c2b0b837c74f454bea914343da9f29232035aa7632a1b14dc03add9edb /uv /uvx /bin/
# Copy dependency files
COPY --chown=${uid}:${gid} pyproject.toml uv.lock ./
# Create venv and install dependencies (without dev dependencies)
RUN uv sync --frozen --no-dev --no-install-project


# Production image
FROM base AS production
# Copy venv from builder
COPY --from=builder --chown=${uid}:${gid} /app/.venv /app/.venv
# Copy application files
COPY --chown=${uid}:${gid} proxy.py start.sh ./
RUN chmod +x start.sh
# Add venv to PATH
ENV PATH="/app/.venv/bin:$PATH"
# Verify uvicorn is available
RUN uvicorn --version
# Create tailscale state directory with correct permissions
RUN mkdir -p /var/run/tailscale && chown ${uid}:${gid} /var/run/tailscale
# Switch to non-root user
USER ${uid}:${gid}

ENTRYPOINT ["./start.sh"]
