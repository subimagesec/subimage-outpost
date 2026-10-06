# syntax=docker/dockerfile:1.7

# Pin updated by Dependabot (docker ecosystem, /).
FROM tailscale/tailscale:v1.102.5@sha256:c507f3a2a6ab1cabd8d809b98edeb41edbd5c3fb6ad9632ffd098b4c7d0b4065 AS tailscale

# Base image
FROM python:3.14.7-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS base
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
