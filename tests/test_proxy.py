import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient


def load_proxy(monkeypatch, token_path: Path | None = None, token: str | None = None):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("PROXY_TARGET", "https://kubernetes.default.svc")
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    monkeypatch.delenv("BEARER_TOKEN_PATH", raising=False)
    monkeypatch.delenv("CA_BUNDLE", raising=False)

    if token is not None:
        monkeypatch.setenv("BEARER_TOKEN", token)
    if token_path is not None:
        monkeypatch.setenv("BEARER_TOKEN_PATH", str(token_path))

    sys.modules.pop("proxy", None)
    import proxy

    return proxy


def test_file_backed_bearer_token_is_reloaded(monkeypatch, tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("first-token")

    proxy = load_proxy(monkeypatch, token_path=token_path)

    assert proxy.get_bearer_token() == "first-token"

    token_path.write_text("rotated-token")

    assert proxy.get_bearer_token() == "rotated-token"


def test_static_bearer_token_takes_precedence(monkeypatch, tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("file-token")

    proxy = load_proxy(
        monkeypatch,
        token_path=token_path,
        token="static-token",
    )

    assert proxy.get_bearer_token() == "static-token"

    token_path.write_text("rotated-token")

    assert proxy.get_bearer_token() == "static-token"


def test_missing_bearer_token_file_returns_none(monkeypatch, tmp_path):
    proxy = load_proxy(monkeypatch, token_path=tmp_path / "missing-token")

    assert proxy.get_bearer_token() is None


def test_file_backed_bearer_token_can_appear_after_startup(monkeypatch, tmp_path):
    token_path = tmp_path / "late-token"

    proxy = load_proxy(monkeypatch, token_path=token_path)

    assert proxy.get_bearer_token() is None

    token_path.write_text("late-token")

    assert proxy.get_bearer_token() == "late-token"


def test_empty_bearer_token_file_returns_none(monkeypatch, tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("first-token")

    proxy = load_proxy(monkeypatch, token_path=token_path)

    assert proxy.get_bearer_token() == "first-token"

    token_path.write_text("")

    assert proxy.get_bearer_token() is None


def test_bearer_token_read_error_uses_cached_token(monkeypatch, tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("first-token")

    proxy = load_proxy(monkeypatch, token_path=token_path)

    assert proxy.get_bearer_token() == "first-token"

    token_path.write_text("rotated-token")

    def raise_permission_error(self, *args, **kwargs):
        raise PermissionError("cannot read token")

    monkeypatch.setattr(
        type(proxy.BEARER_TOKEN_PATH), "read_text", raise_permission_error
    )

    assert proxy.get_bearer_token() == "first-token"


def test_build_proxy_headers_uses_rotated_file_backed_token(monkeypatch, tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("first-token")

    proxy = load_proxy(monkeypatch, token_path=token_path)

    assert proxy.build_proxy_headers({})["Authorization"] == "Bearer first-token"

    token_path.write_text("rotated-token")

    assert proxy.build_proxy_headers({})["Authorization"] == "Bearer rotated-token"


def test_build_proxy_headers_preserves_existing_authorization(monkeypatch, tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("file-token")

    proxy = load_proxy(monkeypatch, token_path=token_path)

    headers = proxy.build_proxy_headers({"Authorization": "Bearer caller-token"})

    assert headers["Authorization"] == "Bearer caller-token"


def test_build_proxy_headers_preserves_lowercase_authorization(monkeypatch, tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("file-token")

    proxy = load_proxy(monkeypatch, token_path=token_path)

    headers = proxy.build_proxy_headers({"authorization": "Bearer caller-token"})

    assert headers["authorization"] == "Bearer caller-token"
    assert "Authorization" not in headers


def test_version_endpoint(monkeypatch):
    monkeypatch.setenv("OUTPOST_VERSION", "9.9.9")

    proxy = load_proxy(monkeypatch)
    client = TestClient(proxy.app)

    response = client.get("/_internal/version")

    assert response.status_code == 200
    assert response.json() == {"version": "9.9.9"}


def test_logs_pagination(monkeypatch, tmp_path):
    log_file = tmp_path / "outpost.log"
    log_file.write_text("".join(f"line {i}\n" for i in range(10)))
    monkeypatch.setenv("OUTPOST_LOG_FILE", str(log_file))

    proxy = load_proxy(monkeypatch)
    client = TestClient(proxy.app)

    # Default offset returns the tail (last `count` lines).
    tail = client.get("/_internal/logs", params={"count": 3}).json()
    assert tail == {
        "total": 10,
        "offset": 7,
        "count": 3,
        "lines": ["line 7", "line 8", "line 9"],
    }

    # Explicit offset returns the [offset, offset + count) window in file order.
    page = client.get("/_internal/logs", params={"count": 2, "offset": 0}).json()
    assert page == {
        "total": 10,
        "offset": 0,
        "count": 2,
        "lines": ["line 0", "line 1"],
    }

    # Offset past EOF yields an empty window but still reports the total.
    past = client.get("/_internal/logs", params={"count": 5, "offset": 50}).json()
    assert past == {"total": 10, "offset": 50, "count": 0, "lines": []}


def test_logs_reads_rotated_files_in_order(monkeypatch, tmp_path):
    # logtee.py keeps `<name>` (active) plus `<name>.1` (newest backup) ..
    # `<name>.N` (oldest). The endpoint must stitch them oldest-first.
    log_file = tmp_path / "outpost.log"
    (tmp_path / "outpost.log.2").write_text("old 0\nold 1\n")
    (tmp_path / "outpost.log.1").write_text("mid 0\nmid 1\n")
    log_file.write_text("new 0\nnew 1\n")
    monkeypatch.setenv("OUTPOST_LOG_FILE", str(log_file))

    proxy = load_proxy(monkeypatch)
    client = TestClient(proxy.app)

    body = client.get("/_internal/logs", params={"count": 100, "offset": 0}).json()

    assert body["total"] == 6
    assert body["lines"] == [
        "old 0",
        "old 1",
        "mid 0",
        "mid 1",
        "new 0",
        "new 1",
    ]


def test_logtee_tees_and_rotates(tmp_path):
    log_file = tmp_path / "outpost.log"
    script = Path(__file__).resolve().parents[1] / "logtee.py"
    env = {
        **os.environ,
        # Tiny cap to force several rotations and prove disk stays bounded.
        "OUTPOST_LOG_MAX_BYTES": "40",
        "OUTPOST_LOG_BACKUP_COUNT": "2",
    }
    payload = "".join(f"line {i}\n" for i in range(30))

    result = subprocess.run(
        [sys.executable, str(script), str(log_file)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    # Console (stdout) still sees every line.
    assert "line 0" in result.stdout
    assert "line 29" in result.stdout

    # Rotation happened and the number of files is bounded to backupCount + 1.
    log_files = sorted(p.name for p in tmp_path.glob("outpost.log*"))
    assert log_files == ["outpost.log", "outpost.log.1", "outpost.log.2"]

    # The active file holds the most recent line.
    assert "line 29" in log_file.read_text()


def test_logs_missing_file_is_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTPOST_LOG_FILE", str(tmp_path / "missing.log"))

    proxy = load_proxy(monkeypatch)
    client = TestClient(proxy.app)

    body = client.get("/_internal/logs").json()

    assert body == {"total": 0, "offset": 0, "count": 0, "lines": []}


def test_logs_rejects_invalid_params(monkeypatch, tmp_path):
    log_file = tmp_path / "outpost.log"
    log_file.write_text("line\n")
    monkeypatch.setenv("OUTPOST_LOG_FILE", str(log_file))

    proxy = load_proxy(monkeypatch)
    client = TestClient(proxy.app)

    assert client.get("/_internal/logs", params={"count": 0}).status_code == 400
    assert client.get("/_internal/logs", params={"offset": -1}).status_code == 400


def test_internal_routes_not_proxied(monkeypatch):
    proxy = load_proxy(monkeypatch)
    client = TestClient(proxy.app)

    # /_internal/version must be served locally; if it fell through to the
    # catch-all proxy it would try to reach PROXY_TARGET and not return our body.
    response = client.get("/_internal/version")

    assert response.status_code == 200
    assert "version" in response.json()
