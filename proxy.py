# proxy.py
import os
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi import Request
from fastapi import Response

app = FastAPI()

TARGET = os.environ.get("PROXY_TARGET")  # e.g. "https://snipeit.internal.local"
TARGET_HOST = os.environ.get("PROXY_HOST")  # optional: override Host header
VERIFY_TLS = os.environ.get("VERIFY_TLS", "false").lower() == "true"

if not TARGET:
    raise RuntimeError("PROXY_TARGET must be set, e.g. https://myservice.local")

# Load bearer token from environment variable or file path
BEARER_TOKEN = None

# Option 1: Direct token from environment variable
bearer_token_value = os.environ.get("BEARER_TOKEN")
if bearer_token_value:
    BEARER_TOKEN = bearer_token_value.strip()
    print("Loaded bearer token from BEARER_TOKEN environment variable")

# Option 2: Token from file path (if direct token not provided)
if not BEARER_TOKEN:
    bearer_token_path_str = os.environ.get("BEARER_TOKEN_PATH")
    if bearer_token_path_str:
        bearer_token_path = Path(bearer_token_path_str)
        if bearer_token_path.exists():
            BEARER_TOKEN = bearer_token_path.read_text().strip()
            print(f"Loaded bearer token from file: {bearer_token_path_str}")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    if not TARGET:
        raise RuntimeError("PROXY_TARGET must be set, e.g. https://myservice.local")

    url = f"{TARGET.rstrip('/')}/{path}"
    headers = dict(request.headers)

    # Remove hop-by-hop headers
    headers.pop("host", None)

    if TARGET_HOST:
        headers["Host"] = TARGET_HOST

    headers.setdefault("User-Agent", "Mozilla/5.0 (Tailscale Proxy)")

    # Add bearer token for authentication (e.g., Kubernetes API, internal services)
    # Only add if not already present (allows passing custom Authorization)
    if BEARER_TOKEN and "authorization" not in headers:
        headers["Authorization"] = f"Bearer {BEARER_TOKEN}"

    async with httpx.AsyncClient(verify=VERIFY_TLS) as client:
        proxied_response = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=await request.body(),
            params=request.query_params,
        )

    return Response(
        content=proxied_response.content,
        status_code=proxied_response.status_code,
        headers=proxied_response.headers,
    )
