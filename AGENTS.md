# AGENTS.md

## Cursor Cloud specific instructions

This repo is **SubImage Outpost**: a single Python 3.13+ FastAPI reverse proxy (`proxy.py`)
managed with [`uv`](https://docs.astral.sh/uv/). In production it runs inside one Docker
container (`start.sh`) alongside `tailscaled` + `tailscale serve`, but for local
development you only need the FastAPI proxy — no Tailscale, Docker, or database.

### Environment
- Dependencies are installed via `uv sync --frozen` (handled by the startup update script).
- `uv` manages the interpreter; it resolves to Python 3.14.x here even though
  `pyproject.toml` only requires `>=3.13`. `uv` is on `PATH` for interactive shells
  (sourced from `~/.local/bin/env` in `~/.bashrc`).

### Lint / test / run (all via `uv run`)
- Lint: `make test` (alias for `uv run --frozen pre-commit run --all-files --show-diff-on-failure`).
  The first pre-commit run downloads hook environments and needs network access.
- Tests: `uv run --frozen pytest -q`. The suite imports `proxy` with a dummy
  `PROXY_TARGET` and uses `fastapi.testclient`; it needs no external services.
- Run proxy (dev): `PROXY_TARGET=<url> uv run uvicorn proxy:app --host 127.0.0.1 --port 8080`.

### Gotchas
- `proxy.py` raises `RuntimeError` at **import time** if `PROXY_TARGET` is unset, so it
  must be set before starting uvicorn or importing the module directly.
- uvicorn binds to `127.0.0.1` by design (the only intended ingress is `tailscale serve`).
- Internal endpoints `GET /_internal/version` and `GET /_internal/logs` are served
  locally; every other path is proxied to `PROXY_TARGET`.
- Full end-to-end tunneling requires a real `TAILSCALE_AUTHKEY` + reachable internal
  target and cannot be exercised locally; validate the proxy standalone instead.
