import os
import ssl
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi import Request
from fastapi import Response

app = FastAPI()

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
STATIC_BEARER_TOKEN = None
BEARER_TOKEN_PATH = None

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
    if STATIC_BEARER_TOKEN:
        return STATIC_BEARER_TOKEN

    if BEARER_TOKEN_PATH is None:
        return None

    if not BEARER_TOKEN_PATH.exists():
        return None

    return BEARER_TOKEN_PATH.read_text().strip()


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    if not TARGET:
        raise RuntimeError("PROXY_TARGET must be set, e.g. https://myservice.local")

    url = f"{TARGET.rstrip('/')}/{path}"
    # Filter hop-by-hop headers from request (case-insensitive)
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS and k.lower() != "host"
    }

    if TARGET_HOST:
        headers["Host"] = TARGET_HOST

    headers.setdefault("User-Agent", "Mozilla/5.0 (Tailscale Proxy)")

    # Add bearer token for authentication (e.g., Kubernetes API, internal services)
    # Only add if not already present (allows passing custom Authorization)
    bearer_token = get_bearer_token()
    if bearer_token and "authorization" not in headers:
        headers["Authorization"] = f"Bearer {bearer_token}"

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
