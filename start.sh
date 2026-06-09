#!/bin/sh
set -e

# Logout so the ephemeral node is removed immediately instead of being
# tombstoned ~1h. Trap INT/TERM so the RPC runs on signal-driven shutdowns
# (set -e + wait below then triggers EXIT — _cleaned guards the second pass).
# The sleep lets tailscaled flush the logout before it gets reaped.
_cleaned=0
cleanup() {
    [ "$_cleaned" = "1" ] && return 0
    _cleaned=1
    echo "Shutting down outpost..."
    tailscale logout || true
    sleep 3
}
trap cleanup INT TERM EXIT

# Tee all stdout/stderr to a size-rotated log file so the /_internal/logs
# endpoint can read it back (bounded on disk), while still printing to the
# console for `kubectl/docker logs`. /bin/sh is dash (no process substitution),
# so use the FIFO + background reader idiom; logtee.py does the rotation + tee.
export OUTPOST_LOG_FILE="${OUTPOST_LOG_FILE:-/tmp/outpost.log}"
_log_pipe="$(mktemp -u)"
mkfifo "$_log_pipe"
python3 /app/logtee.py "$OUTPOST_LOG_FILE" < "$_log_pipe" &
exec > "$_log_pipe" 2>&1
rm -f "$_log_pipe"

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
cd /app && uvicorn proxy:app --host 127.0.0.1 --port ${PROXY_PORT} &

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
