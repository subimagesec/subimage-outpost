# Subimage Outpost

Securely expose internal HTTP(S) APIs from a customer's private network to our tailnet

This container runs:
- `tailscaled` in userspace mode to join the tailnet
- A FastAPI proxy to forward API requests to an internal IP or DNS
- `tailscale serve` to expose the API over a fixed tailnet URL

## Required Environment Variables

| Variable              | Description                                                  |
|-----------------------|--------------------------------------------------------------|
| `TAILSCALE_AUTHKEY`   | OAuth client secret from Subimage                            |
| `TENANT_ID`           | Your tenant ID (provided by Subimage)                        |
| `PROXY_TARGET`        | Full target URL to proxy (e.g. `https://snipeit.internal`)   |

## Optional Environment Variables

| Variable              | Description                                                  | Default     |
|-----------------------|--------------------------------------------------------------|-------------|
| `ENVIRONMENT`         | Environment (e.g. `prod`, `staging`)                        | `prod`      |
| `NAME`                | Name for this outpost (required if using multiple outposts within a tenant)    | `subimage`  |
| `PROXY_HOST`          | Overrides the Host header sent to target                    | _None_      |
| `VERIFY_TLS`          | Verify TLS certs (`true` or `false`)                        | `false`     |
| `CA_BUNDLE`           | Path to a CA bundle file used to verify TLS (only honored when `VERIFY_TLS=true`). If unset, defaults to the Kubernetes serviceaccount CA at `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt` when that file exists. | _None_ |
| `BEARER_TOKEN`        | Bearer token for authentication (added to Authorization header) | _None_  |
| `BEARER_TOKEN_PATH`   | Path to file containing bearer token                        | _None_      |
| `PROXY_CONNECT_TIMEOUT` | Seconds to wait for a TCP/TLS connection to `PROXY_TARGET`. Raise it when cluster DNS is slow (a degraded CoreDNS with `ndots:5` search-domain fanout can take a while to resolve `kubernetes.default.svc`). | `15` |
| `PROXY_READ_TIMEOUT`  | Seconds to wait for the target's response, or for the next chunk of a streamed Git response; also applied to the write and pool timeouts. Raise it for large `list` calls against a big cluster. | `60` |

The `TENANT_ID`, `ENVIRONMENT`, and `NAME` automatically configure:
- Tailscale hostname: `{TENANT_ID}-{NAME}-outpost`
- Tailscale tags: `tag:{TENANT_ID}-{ENVIRONMENT}-outpost`

### Authentication

The proxy supports automatic authentication via bearer tokens:

- **`BEARER_TOKEN`**: Provide the token directly as an environment variable
- **`BEARER_TOKEN_PATH`**: Provide a path to a file containing the token (e.g., Kubernetes ServiceAccount token). File-backed tokens are reloaded for each proxied request so projected Kubernetes ServiceAccount token rotation is honored.

If either variable is set, the proxy automatically adds an `Authorization: Bearer <token>` header to all proxied requests (unless the request already contains an Authorization header).

**Use cases:**
- **Kubernetes API**: When deployed via Helm with RBAC enabled, `BEARER_TOKEN_PATH` is automatically set to `/var/run/secrets/kubernetes.io/serviceaccount/token`
- **Internal services**: Pass a static token via `BEARER_TOKEN` or mount a token file and use `BEARER_TOKEN_PATH`
- **No auth**: If neither variable is set, requests are forwarded without authentication

### Git over the outpost

From version 1.2.5, you can run `git clone`, `git fetch` and `git push` through the outpost against a Git smart-HTTP server, such as GitHub Enterprise Server.

The outpost recognizes Git traffic by its URL: `GET .../info/refs?service=git-upload-pack` or `?service=git-receive-pack`, and `POST .../git-upload-pack` or `.../git-receive-pack`. For these requests, the outpost streams the request and the response in both directions and passes them through byte for byte. It keeps `Accept-Encoding`, `Content-Encoding` and `Content-Length` as the client and the target sent them, so a pack of any size never sits in memory. If the client sends no `Accept-Encoding`, the outpost sends `Accept-Encoding: identity`, so the target answers uncompressed.

`PROXY_READ_TIMEOUT` limits how long the outpost waits for the next chunk of data, not the whole transfer. A long clone keeps running while data flows.

If the target fails before it answers, you get the usual JSON error with status 502 or 504. If the target fails after the response starts, the outpost closes the connection before the body ends, and Git reports the transfer as failed.

To check that an outpost supports Git, call `GET /_internal/version` and confirm that `version` is `1.2.5` or later.

### Compressed responses

From version 1.2.7, the outpost passes compressed responses through to clients that accept gzip. If the `Accept-Encoding` of a request lists `gzip` with a quality above zero (`gzip;q=0` doesn't count), the outpost handles it the way it handles Git traffic: it forwards the client's `Accept-Encoding`, then streams the target's body back with its `Content-Encoding` and `Content-Length`. A Kubernetes API server gzips large responses, so a big `list` call crosses the tailnet at about a tenth of its size.

If the `Accept-Encoding` of a request doesn't accept `gzip`, or the request has none, the outpost buffers the request, asks the target for `gzip, deflate`, and returns the decoded body without `Content-Encoding`.

To check that an outpost passes compressed responses through, call `GET /_internal/version` and confirm that `version` is `1.2.7` or later.

---

## Examples

### Single outpost (default)

```bash
docker build -t proxy .

docker run -d --restart unless-stopped \
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

docker run -d --restart unless-stopped \
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

docker run -d --restart unless-stopped \
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

### Restarts

The container exits when its proxy process dies, so run it under a restart policy, as the examples above do: `--restart unless-stopped` with `docker run` (instead of `--rm`), `restart: unless-stopped` in Docker Compose, or a Deployment on Kubernetes. Without one, the outpost stays down until someone restarts it.

---

## Releasing a New Version

This project uses [semantic versioning](https://semver.org):

```bash
make release VERSION=0.0.1
```

When you push a tag, GitHub Actions builds a multi-architecture image (amd64 and arm64), pushes it to `ghcr.io/subimagesec/subimage-outpost`, and attests it. It then publishes the GitHub release with the image digest in the notes. If the build fails, no release is published.

A published release locks its version: you can't move or reuse its git tag. To change a release, publish a new version.

## Architecture Support

The container image supports both x86_64 (amd64) and ARM64 architectures. Docker will automatically pull the appropriate image variant based on your host architecture.

## Verify the image

A version tag such as `1.2.6` is a pointer that the registry lets a publisher move to a different image. A digest identifies one image and never changes. To run the exact image that Subimage released, verify the image, and then pull it by digest.

Starting with version 1.2.6, GitHub Actions signs a record of each image that it builds from this repository. Earlier versions have no record to verify.

1. Find the digest. Each release lists it at the top of its release notes.

1. Verify that this repository's release workflow built the image:

   ```bash
   gh attestation verify oci://ghcr.io/subimagesec/subimage-outpost@DIGEST \
     --repo subimagesec/subimage-outpost \
     --signer-workflow subimagesec/subimage-outpost/.github/workflows/release.yml
   ```

1. Pull the image by digest:

   ```bash
   docker pull ghcr.io/subimagesec/subimage-outpost@DIGEST
   ```

Replace `DIGEST` with the digest from the release notes, including its `sha256:` prefix.

---
## Why Subimage Outpost?
1. Requires no inbound firewall changes.
1. Encrypted via WireGuard automatically.
1. Requires no changes to Cartography ingestion
1. With direct connections, Tailscale is FAST
