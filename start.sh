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
    # A startup that fails before tailscaled runs has no node to log out. The
    # sleep still runs so logtee flushes the error before the container exits.
    if [ -S /var/run/tailscale/tailscaled.sock ]; then
        tailscale logout || true
    fi
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

# Workload identity federation joins the tailnet with a short-lived token from
# the platform the outpost runs on, so no long-lived secret has to be handed
# over. The registration key stays supported for platforms without a public
# OIDC issuer. The auth arguments go in "$@" rather than a string so a key such
# as `tskey-client-...?ephemeral=true` is never word-split or globbed.
if [ -n "${TAILSCALE_CLIENT_ID}" ]; then
  if [ -n "${TAILSCALE_AUTHKEY}" ]; then
    echo "ERROR: Set TAILSCALE_CLIENT_ID for workload identity or TAILSCALE_AUTHKEY for a registration key, not both"
    exit 1
  fi
  if [ -n "${TAILSCALE_AUDIENCE}" ] && [ -n "${TAILSCALE_ID_TOKEN_PATH}" ]; then
    echo "ERROR: Set TAILSCALE_AUDIENCE or TAILSCALE_ID_TOKEN_PATH, not both"
    exit 1
  fi
  if [ -n "${TAILSCALE_AUDIENCE}" ]; then
    echo "Authenticating with workload identity (ID token fetched from the cloud provider)"
    set -- --client-id="${TAILSCALE_CLIENT_ID}" --audience="${TAILSCALE_AUDIENCE}"
  elif [ -n "${TAILSCALE_ID_TOKEN_PATH}" ]; then
    if [ ! -r "${TAILSCALE_ID_TOKEN_PATH}" ]; then
      echo "ERROR: TAILSCALE_ID_TOKEN_PATH ${TAILSCALE_ID_TOKEN_PATH} is not a readable file"
      exit 1
    fi
    echo "Authenticating with workload identity (ID token read from ${TAILSCALE_ID_TOKEN_PATH})"
    set -- --client-id="${TAILSCALE_CLIENT_ID}" --id-token="file:${TAILSCALE_ID_TOKEN_PATH}"
  else
    echo "ERROR: TAILSCALE_CLIENT_ID needs TAILSCALE_AUDIENCE or TAILSCALE_ID_TOKEN_PATH"
    exit 1
  fi
elif [ -n "${TAILSCALE_AUTHKEY}" ]; then
  echo "Authenticating with a registration key"
  set -- --auth-key="${TAILSCALE_AUTHKEY}"
else
  echo "ERROR: Set TAILSCALE_CLIENT_ID (workload identity) or TAILSCALE_AUTHKEY (registration key)"
  exit 1
fi

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
tailscale up "$@" --hostname="${TAILSCALE_HOSTNAME:-proxy}" --accept-routes ${TS_EXTRA_ARGS}
echo "Connected to Tailscale"

# Start proxy
echo "Starting proxy server on port ${PROXY_PORT}..."
cd /app && uvicorn proxy:app --host 127.0.0.1 --port ${PROXY_PORT} &
PROXY_PID=$!

sleep 2

# Expose via tailnet
echo "Exposing proxy via Tailscale serve..."
tailscale serve --bg --http ${TAILSCALE_SERVE_PORT} http://localhost:${PROXY_PORT}
echo "Outpost is ready and serving"

# Once the proxy is gone, tailscale serve answers every request with an empty
# 502 while the node stays online, so the outpost looks healthy from the tailnet
# and fails every sync until someone restarts it. Exit with the proxy instead so
# the container's restart policy brings it back. wait also returns when a signal
# interrupts it, after the trap has run.
proxy_status=0
wait "$PROXY_PID" || proxy_status=$?
if [ "$_cleaned" = "1" ]; then
    exit "$proxy_status"
fi
echo "ERROR: proxy exited with status ${proxy_status}; stopping the outpost so it restarts"
exit 1
