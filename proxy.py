import logging
import math
import os
import ssl
from collections.abc import AsyncIterator
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import asynccontextmanager
from itertools import islice
from pathlib import Path
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import httpx
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# Build-time version, injected via the OUTPOST_VERSION env var (set from the git
# tag in the Dockerfile). Falls back to 0.0.0 for local/dev runs.
OUTPOST_VERSION = os.environ.get("OUTPOST_VERSION", "0.0.0")

# Container stdout is tee'd here by start.sh; the /_internal/logs endpoint reads
# it back. Kept in sync via the same env var in start.sh.
OUTPOST_LOG_FILE = Path(os.environ.get("OUTPOST_LOG_FILE", "/tmp/outpost.log"))

# Bound the work the logs endpoint will do per request.
DEFAULT_LOG_COUNT = 100
MAX_LOG_COUNT = 1000

# Hop-by-hop headers (RFC 2616 Section 13.5.1) must not be forwarded by proxies
HOP_BY_HOP_HEADERS = frozenset(
    [
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    ]
)

TARGET = os.environ.get("PROXY_TARGET")  # e.g. "https://snipeit.internal.local"
TARGET_HOST = os.environ.get("PROXY_HOST")  # optional: override Host header
VERIFY_TLS = os.environ.get("VERIFY_TLS", "false").lower() == "true"

# Same derivation start.sh uses for the tailnet hostname, recomputed here (it is
# only a shell local there) so upstream failures can name the outpost the
# operator sees on the outposts settings page. start.sh only builds a hostname
# when TENANT_ID is set and otherwise passes `--hostname proxy`, so the fallback
# has to be "proxy" and not NAME, which would name a node nobody can find.
OUTPOST_NAME = os.environ.get("NAME", "subimage")
TENANT_ID = os.environ.get("TENANT_ID")
OUTPOST_HOSTNAME = f"{TENANT_ID}-{OUTPOST_NAME}-outpost" if TENANT_ID else "proxy"

# Default to the in-cluster Kubernetes serviceaccount CA bundle if present;
# users can override with CA_BUNDLE for non-kube self-signed targets.
DEFAULT_K8S_CA_BUNDLE = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
ca_bundle_env = os.environ.get("CA_BUNDLE")
if ca_bundle_env:
    CA_BUNDLE_PATH: Path | None = Path(ca_bundle_env)
elif Path(DEFAULT_K8S_CA_BUNDLE).is_file():
    CA_BUNDLE_PATH = Path(DEFAULT_K8S_CA_BUNDLE)
else:
    CA_BUNDLE_PATH = None

if VERIFY_TLS and CA_BUNDLE_PATH is not None:
    if not CA_BUNDLE_PATH.is_file():
        raise RuntimeError(f"CA_BUNDLE path does not exist: {CA_BUNDLE_PATH}")
    VERIFY: bool | ssl.SSLContext = ssl.create_default_context(
        cafile=str(CA_BUNDLE_PATH)
    )
    print(f"Loaded CA bundle for TLS verification: {CA_BUNDLE_PATH}")
else:
    VERIFY = VERIFY_TLS

if not TARGET:
    raise RuntimeError("PROXY_TARGET must be set, e.g. https://myservice.local")


def _sanitize_target(target: str) -> str:
    """Strip anything secret-shaped out of PROXY_TARGET before it is echoed.

    Auth is meant to travel via BEARER_TOKEN, but nothing stops a target from
    carrying URL userinfo or a token in its query string, and upstream failures
    put this value in both the response body and OUTPOST_LOG_FILE, which is
    served by /_internal/logs and rendered on the outposts settings page.
    Scheme, host, port and path are enough to diagnose a connection failure, so
    drop userinfo, query and fragment wholesale rather than guessing which
    parameters are sensitive.
    """
    try:
        parts = urlsplit(target)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return "<unparseable PROXY_TARGET>"

    if not parts.scheme or not hostname:
        # Not a URL we can take apart, so do not risk echoing it verbatim.
        return "<redacted PROXY_TARGET>"

    netloc = f"{hostname}:{port}" if port else hostname
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


SAFE_TARGET = _sanitize_target(TARGET)


def _positive_float_env(name: str, default: float) -> float:
    """Read a finite positive float from the environment, falling back on bad input.

    A typo here should not crashloop an otherwise-working outpost, so this warns
    and uses the default rather than raising the way the CA bundle check does.
    float() also accepts "nan" and "inf", which would sail past a plain `<= 0`
    check and reach httpx as a timeout that never fires.
    """
    raw = os.environ.get(name)
    if not raw:
        return default

    try:
        value = float(raw)
    except ValueError:
        value = 0.0

    if not math.isfinite(value) or value <= 0:
        print(f"Ignoring invalid {name}={raw!r}; using {default}")
        return default

    return value


# httpx defaults every phase to 5s. That is shorter than a degraded CoreDNS
# ndots:5 search-domain fanout takes to resolve kubernetes.default.svc, and far
# shorter than a large `list` call across a big cluster. Both are tunable so a
# slow cluster can be adjusted without shipping a new image.
CONNECT_TIMEOUT = _positive_float_env("PROXY_CONNECT_TIMEOUT", 15.0)
READ_TIMEOUT = _positive_float_env("PROXY_READ_TIMEOUT", 60.0)
TIMEOUT = httpx.Timeout(
    connect=CONNECT_TIMEOUT,
    read=READ_TIMEOUT,
    write=READ_TIMEOUT,
    pool=READ_TIMEOUT,
)

# Load static bearer tokens from the environment once. File-backed tokens are
# read per request because Kubernetes projected ServiceAccount tokens rotate.
STATIC_BEARER_TOKEN: str | None = None
BEARER_TOKEN_PATH: Path | None = None
_FILE_BEARER_TOKEN_CACHE: tuple[str, tuple[int, int, int]] | None = None

# Option 1: Direct token from environment variable
bearer_token_value = os.environ.get("BEARER_TOKEN")
if bearer_token_value:
    STATIC_BEARER_TOKEN = bearer_token_value.strip()
    print("Loaded bearer token from BEARER_TOKEN environment variable")

# Option 2: Token from file path (if direct token not provided)
if not STATIC_BEARER_TOKEN:
    bearer_token_path_str = os.environ.get("BEARER_TOKEN_PATH")
    if bearer_token_path_str:
        BEARER_TOKEN_PATH = Path(bearer_token_path_str)
        if BEARER_TOKEN_PATH.exists():
            print(f"Configured bearer token file: {bearer_token_path_str}")


def get_bearer_token() -> str | None:
    global _FILE_BEARER_TOKEN_CACHE

    if STATIC_BEARER_TOKEN:
        return STATIC_BEARER_TOKEN

    if BEARER_TOKEN_PATH is None:
        return None

    try:
        file_stat = BEARER_TOKEN_PATH.stat()
    except OSError:
        if _FILE_BEARER_TOKEN_CACHE:
            logger.warning("Unable to stat bearer token file; using cached token")
            return _FILE_BEARER_TOKEN_CACHE[0]
        else:
            logger.warning("Unable to stat bearer token file")
            return None

    cache_key = (file_stat.st_mtime_ns, file_stat.st_size, file_stat.st_ino)

    if _FILE_BEARER_TOKEN_CACHE and cache_key == _FILE_BEARER_TOKEN_CACHE[1]:
        return _FILE_BEARER_TOKEN_CACHE[0]

    try:
        token = BEARER_TOKEN_PATH.read_text().strip()
    except OSError:
        if _FILE_BEARER_TOKEN_CACHE:
            logger.warning("Unable to read bearer token file; using cached token")
            return _FILE_BEARER_TOKEN_CACHE[0]
        else:
            logger.warning("Unable to read bearer token file")
            return None

    if not token:
        logger.warning("Bearer token file is empty")
        return None

    _FILE_BEARER_TOKEN_CACHE = (token, cache_key)
    return token


def build_proxy_headers(request_headers: Mapping[str, str]) -> dict[str, str]:
    headers = {
        k: v
        for k, v in request_headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS and k.lower() != "host"
    }

    if TARGET_HOST:
        headers["Host"] = TARGET_HOST

    headers.setdefault("User-Agent", "Mozilla/5.0 (Tailscale Proxy)")

    bearer_token = get_bearer_token()
    has_authorization = any(k.lower() == "authorization" for k in headers)
    if bearer_token and not has_authorization:
        headers["Authorization"] = f"Bearer {bearer_token}"

    return headers


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Hold a single httpx client for the process lifetime.

    Building one per request meant a fresh TLS handshake on every proxied call
    and no connection pooling at all, which a Cartography sync pays for on every
    one of its many sequential API calls.
    """
    async with httpx.AsyncClient(verify=VERIFY, timeout=TIMEOUT) as client:
        app.state.client = client
        yield


app = FastAPI(lifespan=lifespan)


# Internal endpoints. These must be declared before the catch-all proxy route
# below, otherwise it would swallow them. Authentication is handled at the
# network layer: uvicorn binds to 127.0.0.1, so the only ingress is
# `tailscale serve`, meaning every request here comes from a tailnet member.
@app.get("/_internal/version")
async def internal_version() -> dict[str, str]:
    return {"version": OUTPOST_VERSION}


def _ordered_log_files() -> list[Path]:
    """Rotated log files in chronological order (oldest first).

    logtee.py writes via RotatingFileHandler, which keeps the active file plus
    numbered backups (`<name>.1` newest backup ... `<name>.N` oldest).
    """
    base = OUTPOST_LOG_FILE
    backups: list[tuple[int, Path]] = []
    for candidate in base.parent.glob(f"{base.name}.*"):
        suffix = candidate.name[len(base.name) + 1 :]
        if suffix.isdigit():
            backups.append((int(suffix), candidate))
    # Higher backup index == older, so read those first, then the active file.
    ordered = [path for _, path in sorted(backups, reverse=True)]
    ordered.append(base)
    return ordered


def _iter_log_lines() -> Iterator[str]:
    """Stream log lines across all rotated files without loading them all."""
    for path in _ordered_log_files():
        try:
            with path.open("r", errors="replace") as log_file:
                for line in log_file:
                    yield line.rstrip("\n")
        except FileNotFoundError:
            continue


@app.get("/_internal/logs")
async def internal_logs(
    count: int = DEFAULT_LOG_COUNT, offset: int | None = None
) -> dict[str, object]:
    if count < 1:
        raise HTTPException(status_code=400, detail="count must be >= 1")
    if count > MAX_LOG_COUNT:
        count = MAX_LOG_COUNT
    if offset is not None and offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")

    # First pass counts lines (constant memory); the second pass keeps only the
    # requested window via islice, so memory is O(count), not O(total).
    total = sum(1 for _ in _iter_log_lines())
    # Default to the tail: the last `count` lines. The end of the log moves as it
    # grows, so this is recomputed per request.
    resolved_offset = max(0, total - count) if offset is None else offset
    window = list(islice(_iter_log_lines(), resolved_offset, resolved_offset + count))

    return {
        "total": total,
        "offset": resolved_offset,
        "count": len(window),
        "lines": window,
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    if not TARGET:
        raise RuntimeError("PROXY_TARGET must be set, e.g. https://myservice.local")

    url = f"{TARGET.rstrip('/')}/{path}"
    headers = build_proxy_headers(request.headers)
    client: httpx.AsyncClient = request.app.state.client

    try:
        proxied_response = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=await request.body(),
            params=request.query_params,
        )
    # InvalidURL is a bare Exception rather than a TransportError, so a
    # PROXY_TARGET with something like a bad port would otherwise escape as the
    # opaque 500 this handler exists to eliminate.
    except (httpx.TransportError, httpx.InvalidURL) as exc:
        # Never getting an answer from upstream (timed out, refused, or the name
        # would not resolve) is a gateway timeout; any other transport-level
        # failure is a bad gateway.
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
            status_code = 504
            message = (
                "Outpost could not reach its proxy target "
                f"(connect timeout {CONNECT_TIMEOUT}s, read timeout {READ_TIMEOUT}s)"
            )
        elif isinstance(exc, httpx.InvalidURL):
            # Points at the configuration rather than the network, which is a
            # different fix for whoever reads this.
            status_code = 502
            message = "Outpost could not build a valid URL for its proxy target"
        else:
            status_code = 502
            message = "Outpost failed to proxy the request to its target"

        # httpx raises some of these with an empty message, so only append one
        # when there is something to say.
        detail = type(exc).__name__
        if str(exc):
            detail = f"{detail}: {exc}"

        # A warning, not a traceback: this is tee'd to OUTPOST_LOG_FILE and read
        # back through /_internal/logs and the outposts settings page.
        logger.warning("%s: %s: %s", message, SAFE_TARGET, detail)

        # Deliberately not a Kubernetes `Status` object: the backend tells an
        # outpost-side failure apart from a genuine API server 5xx by checking
        # that the body is not one (see _is_outpost_upstream_failure in
        # subimage-backend/app/sync/modules/kubernetes.py).
        return JSONResponse(
            status_code=status_code,
            content={
                "error": message,
                "outpost": OUTPOST_HOSTNAME,
                "target": SAFE_TARGET,
                "detail": detail,
                "version": OUTPOST_VERSION,
            },
        )

    # Filter hop-by-hop headers from response
    response_headers = {
        k: v
        for k, v in proxied_response.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }

    return Response(
        content=proxied_response.content,
        status_code=proxied_response.status_code,
        headers=response_headers,
    )
