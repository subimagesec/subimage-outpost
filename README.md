# SubImage Outpost

Securely expose internal HTTP(S) APIs from a customer's private network to our tailnet

This container runs:
- `tailscaled` in userspace mode to join the tailnet
- A FastAPI proxy to forward API requests to an internal IP or DNS
- `tailscale serve` to expose the API over a fixed tailnet URL

## Required Environment Variables

| Variable              | Description                                                  |
|-----------------------|--------------------------------------------------------------|
| `TAILSCALE_AUTHKEY`   | OAuth client secret from SubImage                            |
| `TENANT_ID`           | Your tenant ID (provided by SubImage)                        |
| `PROXY_TARGET`        | Full target URL to proxy (e.g. `https://snipeit.internal`)   |

## Optional Environment Variables

| Variable              | Description                                                  | Default     |
|-----------------------|--------------------------------------------------------------|-------------|
| `ENVIRONMENT`         | Environment (e.g. `prod`, `staging`)                        | `prod`      |
| `NAME`                | Name for this outpost (required if using multiple outposts within a tenant)    | `subimage`  |
| `PROXY_HOST`          | Overrides the Host header sent to target                    | _None_      |
| `VERIFY_TLS`          | Verify TLS certs (`true` or `false`)                        | `false`     |
| `BEARER_TOKEN`        | Bearer token for authentication (added to Authorization header) | _None_  |
| `BEARER_TOKEN_PATH`   | Path to file containing bearer token                        | _None_      |

The `TENANT_ID`, `ENVIRONMENT`, and `NAME` automatically configure:
- Tailscale hostname: `{TENANT_ID}-{NAME}-outpost`
- Tailscale tags: `tag:{TENANT_ID}-{ENVIRONMENT}-outpost`

### Authentication

The proxy supports automatic authentication via bearer tokens:

- **`BEARER_TOKEN`**: Provide the token directly as an environment variable
- **`BEARER_TOKEN_PATH`**: Provide a path to a file containing the token (e.g., Kubernetes ServiceAccount token)

If either variable is set, the proxy automatically adds an `Authorization: Bearer <token>` header to all proxied requests (unless the request already contains an Authorization header).

**Use cases:**
- **Kubernetes API**: When deployed via Helm with RBAC enabled, `BEARER_TOKEN_PATH` is automatically set to `/var/run/secrets/kubernetes.io/serviceaccount/token`
- **Internal services**: Pass a static token via `BEARER_TOKEN` or mount a token file and use `BEARER_TOKEN_PATH`
- **No auth**: If neither variable is set, requests are forwarded without authentication

---

## Examples

### Single outpost (default)

```bash
docker build -t proxy .

docker run --rm \
  -e TAILSCALE_AUTHKEY=tskey-client-abc123?ephemeral=true \
  -e TENANT_ID=acme \
  -e ENVIRONMENT=prod \
  -e PROXY_TARGET=https://snipeit.internal \
  -e VERIFY_TLS=true \
  proxy
```

Hostname: `acme-subimage-outpost`
Tags: `tag:acme-prod-outpost`

### Multiple outposts

Deploy separate outposts for different networks/services:

**EKS in production VPC:**
```bash
docker build -t proxy .

docker run --rm \
  -e TAILSCALE_AUTHKEY=tskey-client-abc123?ephemeral=true \
  -e TENANT_ID=acme \
  -e ENVIRONMENT=prod \
  -e NAME=eks-prod \
  -e PROXY_TARGET=https://eks.internal.acme.com \
  proxy
```
Hostname: `acme-eks-prod-outpost`
Tags: `tag:acme-prod-outpost`

**IT services (SnipeIT, Jamf) in corporate VPC:**
```bash
docker build -t proxy .

docker run --rm \
  -e TAILSCALE_AUTHKEY=tskey-client-abc123?ephemeral=true \
  -e TENANT_ID=acme \
  -e ENVIRONMENT=prod \
  -e NAME=it \
  -e PROXY_TARGET=https://snipeit.corp.acme.com \
  proxy
```
Hostname: `acme-it-outpost`
Tags: `tag:acme-prod-outpost`

All outposts for a tenant+environment share the same OAuth secret and Tailscale tag - just use different `NAME` values to get unique hostnames.

---

## Releasing a New Version

This project uses [semantic versioning](https://semver.org):

```bash
make release VERSION=0.0.1
```

When a new tag is pushed, GitHub Actions automatically builds and pushes a multi-architecture Docker image (amd64 and arm64) to our ECR repository.

## Architecture Support

The container image supports both x86_64 (amd64) and ARM64 architectures. Docker will automatically pull the appropriate image variant based on your host architecture.

---
## Why SubImage Outpost?
1. Requires no inbound firewall changes.
1. Encrypted via WireGuard automatically.
1. Requires no changes to Cartography ingestion
1. With direction connections, Tailscale is FAST
