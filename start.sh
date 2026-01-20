#!/bin/sh
set -e

# Clean shutdown handler - logout from Tailscale to remove ephemeral node immediately
# Using signal numbers (15=SIGTERM, 2=SIGINT) for POSIX sh compatibility
cleanup() {
    echo "Shutting down outpost..."
    tailscale logout || true
    exit 0
}
trap cleanup 15 2

# Config
PROXY_PORT="8080"
TAILSCALE_SERVE_PORT="80"

# If ENVIRONMENT is not set, default to "prod"
if [ -z "${ENVIRONMENT}" ]; then
  ENVIRONMENT="prod"
fi

# If TENANT_ID is set, use it to derive hostname and tags
if [ -n "${TENANT_ID}" ]; then
  OUTPOST_NAME="${NAME:-subimage}"
  TAILSCALE_HOSTNAME="${TENANT_ID}-${OUTPOST_NAME}-outpost"
  TS_EXTRA_ARGS="--advertise-tags=tag:${TENANT_ID}-${ENVIRONMENT}-outpost"
fi

# Ensure tailscale socket directory exists
echo "Creating Tailscale socket directory..."
mkdir -p /var/run/tailscale

# Start tailscaled in userspace mode
echo "Starting tailscaled in userspace mode..."
tailscaled --tun=userspace-networking &

# Wait for tailscaled socket with timeout
echo "Waiting for tailscaled socket..."
TIMEOUT=60
ELAPSED=0
while [ ! -S /var/run/tailscale/tailscaled.sock ]; do
  if [ $ELAPSED -ge $TIMEOUT ]; then
    echo "ERROR: Tailscaled socket not found after ${TIMEOUT} seconds"
    echo "Directory contents:"
    ls -la /var/run/tailscale/ || echo "Directory does not exist"
    exit 1
  fi
  sleep 1
  ELAPSED=$((ELAPSED + 1))
done
echo "Tailscaled socket ready"

# Connect to the tailnet
echo "Connecting to Tailscale network..."
tailscale up --authkey="${TAILSCALE_AUTHKEY}" --hostname="${TAILSCALE_HOSTNAME:-proxy}" --accept-routes ${TS_EXTRA_ARGS}
echo "Connected to Tailscale"

# Start proxy
echo "Starting proxy server on port ${PROXY_PORT}..."
cd /app && uvicorn proxy:app --host 0.0.0.0 --port ${PROXY_PORT} &

sleep 2

# Expose via tailnet
echo "Exposing proxy via Tailscale serve..."
tailscale serve --bg --http ${TAILSCALE_SERVE_PORT} http://localhost:${PROXY_PORT}
echo "Outpost is ready and serving"

# Keep alive (using wait so trap can catch signals)
while true; do
    sleep 86400 &
    wait $!
done
