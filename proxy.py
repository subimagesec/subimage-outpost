import logging
import os
import ssl
from collections.abc import Iterator
from collections.abc import Mapping
from itertools import islice
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response

app = FastAPI()
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

    async with httpx.AsyncClient(verify=VERIFY) as client:
        proxied_response = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=await request.body(),
            params=request.query_params,
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
