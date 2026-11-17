# syntax=docker/dockerfile:1.7

# Pin updated by Dependabot (docker ecosystem, /).
FROM tailscale/tailscale:v1.104.4@sha256:ed6480b33c6eb67c9cbd203ed42b9fb57e361986725b6856def71b7a3d9e276c AS tailscale

# Base image
FROM python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS base
# UID/GID for non-root user (https://github.com/hexops/dockerfile#do-not-use-a-uid-below-10000)
ARG uid=10001
ARG gid=10001
# Install system dependencies (Tailscale binaries come from the pinned stage above)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl iproute2 iputils-ping ca-certificates && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
COPY --from=tailscale /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscale
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
# Version injected at build time from the git tag, e.g.
# docker build --build-arg VERSION="$(git describe --tags --always)"
ARG VERSION=0.0.0
ENV OUTPOST_VERSION=${VERSION}
# Copy venv from builder
COPY --from=builder --chown=${uid}:${gid} /app/.venv /app/.venv
# Copy application files
COPY --chown=${uid}:${gid} proxy.py logtee.py start.sh ./
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
