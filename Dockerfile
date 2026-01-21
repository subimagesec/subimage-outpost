# Base image
FROM python:3.11.14-slim@sha256:c24e9effa2821a6885165d930d939fec2af0dcf819276138f11dd45e200bd032 AS base
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
COPY --from=ghcr.io/astral-sh/uv:0.7.3@sha256:87a04222b228501907f487b338ca6fc1514a93369bfce6930eb06c8d576e58a4 /uv /uvx /bin/
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
