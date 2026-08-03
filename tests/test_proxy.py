import logging
import os
import subprocess
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient


def load_proxy(
    monkeypatch,
    token_path: Path | None = None,
    token: str | None = None,
    env: dict[str, str] | None = None,
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("PROXY_TARGET", "https://kubernetes.default.svc")
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    monkeypatch.delenv("BEARER_TOKEN_PATH", raising=False)
    monkeypatch.delenv("CA_BUNDLE", raising=False)
    # NAME and TENANT_ID feed the outpost identity in upstream-failure bodies,
    # and NAME is generic enough to leak in from a developer's shell.
    monkeypatch.delenv("NAME", raising=False)
    monkeypatch.delenv("TENANT_ID", raising=False)
    monkeypatch.delenv("PROXY_CONNECT_TIMEOUT", raising=False)
    monkeypatch.delenv("PROXY_READ_TIMEOUT", raising=False)

    if token is not None:
        monkeypatch.setenv("BEARER_TOKEN", token)
    if token_path is not None:
        monkeypatch.setenv("BEARER_TOKEN_PATH", str(token_path))
    # Applied last so a test can set vars the isolation block above clears.
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

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


def raise_on_request(exc: Exception):
    """Stub for AsyncClient.request that always fails with `exc`."""

    async def _request(*args, **kwargs):
        raise exc

    return _request


def test_timeout_defaults(monkeypatch):
    proxy = load_proxy(monkeypatch)

    # Not httpx's 5s default, which is shorter than a degraded CoreDNS lookup.
    assert proxy.TIMEOUT.connect == 15.0
    assert proxy.TIMEOUT.read == 60.0
    assert proxy.TIMEOUT.write == 60.0
    assert proxy.TIMEOUT.pool == 60.0


def test_timeouts_are_env_configurable(monkeypatch):
    proxy = load_proxy(
        monkeypatch,
        env={"PROXY_CONNECT_TIMEOUT": "3", "PROXY_READ_TIMEOUT": "7.5"},
    )

    assert proxy.TIMEOUT.connect == 3.0
    assert proxy.TIMEOUT.read == 7.5
    assert proxy.TIMEOUT.write == 7.5
    assert proxy.TIMEOUT.pool == 7.5


def test_invalid_timeout_falls_back_to_default(monkeypatch):
    # A typo must not crashloop an otherwise-working outpost.
    proxy = load_proxy(
        monkeypatch,
        env={"PROXY_CONNECT_TIMEOUT": "abc", "PROXY_READ_TIMEOUT": "-1"},
    )

    assert proxy.TIMEOUT.connect == 15.0
    assert proxy.TIMEOUT.read == 60.0


def test_non_finite_timeout_falls_back_to_default(monkeypatch):
    # float() happily parses these, and a bare `<= 0` check lets them through
    # to httpx as a timeout that never fires.
    for raw in ("nan", "inf", "-inf", "Infinity"):
        proxy = load_proxy(monkeypatch, env={"PROXY_CONNECT_TIMEOUT": raw})

        assert proxy.TIMEOUT.connect == 15.0, raw


def test_sanitize_target_strips_credentials(monkeypatch):
    proxy = load_proxy(monkeypatch)

    # Userinfo and query strings are the two places a secret can hide.
    assert (
        proxy._sanitize_target("https://user:pa55@snipeit.internal/api?token=abc")
        == "https://snipeit.internal/api"
    )
    # A plain target is passed through unchanged, port included.
    assert (
        proxy._sanitize_target("https://kubernetes.default.svc")
        == "https://kubernetes.default.svc"
    )
    assert proxy._sanitize_target("http://10.0.0.1:6443") == "http://10.0.0.1:6443"
    # urlsplit drops the brackets off an IPv6 literal; rebuilding without them
    # would emit the ambiguous https://::1:8443.
    assert proxy._sanitize_target("https://[::1]:8443/api") == "https://[::1]:8443/api"
    assert proxy._sanitize_target("https://[fd00::1]/api") == "https://[fd00::1]/api"
    # Anything we cannot take apart is never echoed verbatim.
    assert proxy._sanitize_target("not a url") == "<redacted PROXY_TARGET>"


def test_upstream_failure_does_not_leak_target_credentials(monkeypatch, caplog):
    proxy = load_proxy(
        monkeypatch,
        env={"PROXY_TARGET": "https://svc:hunter2@internal.local/base?apikey=s3cret"},
    )

    with TestClient(proxy.app) as client:
        monkeypatch.setattr(
            proxy.app.state.client,
            "request",
            raise_on_request(httpx.ConnectTimeout("timed out")),
        )
        with caplog.at_level(logging.WARNING, logger="proxy"):
            response = client.get("/api/v1/namespaces/kube-system")

    body = response.json()
    assert body["target"] == "https://internal.local/base"

    # The body and the log line are both readable from the settings page.
    serialized = response.text + "".join(r.getMessage() for r in caplog.records)
    assert "hunter2" not in serialized
    assert "s3cret" not in serialized


def test_connect_timeout_returns_504(monkeypatch):
    proxy = load_proxy(monkeypatch)

    with TestClient(proxy.app) as client:
        monkeypatch.setattr(
            proxy.app.state.client,
            "request",
            raise_on_request(httpx.ConnectTimeout("timed out")),
        )
        response = client.get("/api/v1/namespaces/kube-system")

    assert response.status_code == 504
    assert response.headers["content-type"].startswith("application/json")

    body = response.json()
    assert body["target"] == "https://kubernetes.default.svc"
    # start.sh passes `--hostname proxy` when TENANT_ID is unset.
    assert body["outpost"] == "proxy"
    assert "ConnectTimeout" in body["detail"]
    # The backend classifies an outpost 5xx as "Cluster API Server Unreachable"
    # precisely because the body is not a Kubernetes `Status` object.
    assert "kind" not in body


def test_connect_error_and_read_timeout_return_504(monkeypatch):
    for exc in (httpx.ConnectError("name not resolved"), httpx.ReadTimeout("slow")):
        proxy = load_proxy(monkeypatch)

        with TestClient(proxy.app) as client:
            monkeypatch.setattr(
                proxy.app.state.client, "request", raise_on_request(exc)
            )
            response = client.get("/api/v1/namespaces/kube-system")

        assert response.status_code == 504, exc
        assert "kind" not in response.json()


def test_other_transport_error_returns_502(monkeypatch):
    proxy = load_proxy(monkeypatch)

    with TestClient(proxy.app) as client:
        monkeypatch.setattr(
            proxy.app.state.client,
            "request",
            raise_on_request(httpx.RemoteProtocolError("bad chunk")),
        )
        response = client.get("/api/v1/namespaces/kube-system")

    assert response.status_code == 502

    body = response.json()
    assert "RemoteProtocolError" in body["detail"]
    assert "kind" not in body


def test_transport_failure_logs_one_warning_without_traceback(monkeypatch, caplog):
    proxy = load_proxy(monkeypatch)

    with TestClient(proxy.app) as client:
        monkeypatch.setattr(
            proxy.app.state.client,
            "request",
            raise_on_request(httpx.ConnectTimeout("timed out")),
        )
        with caplog.at_level(logging.WARNING, logger="proxy"):
            client.get("/api/v1/namespaces/kube-system")

    records = [record for record in caplog.records if record.name == "proxy"]
    assert len(records) == 1
    assert records[0].exc_info is None
    assert "ConnectTimeout" in records[0].getMessage()


def test_invalid_target_url_returns_502_not_an_opaque_500(monkeypatch):
    # httpx.InvalidURL is a bare Exception, not a TransportError, so a bad port
    # in PROXY_TARGET used to escape the handler as a 500 plus a traceback.
    proxy = load_proxy(
        monkeypatch, env={"PROXY_TARGET": "https://kubernetes.default.svc:notaport"}
    )

    with TestClient(proxy.app) as client:
        response = client.get("/api/v1/namespaces/kube-system")

    assert response.status_code == 502

    body = response.json()
    assert "InvalidURL" in body["detail"]
    # Names the configuration, not the network: a different fix for the reader.
    assert "valid URL" in body["error"]
    assert "kind" not in body


def test_outpost_identity_uses_tailnet_hostname(monkeypatch):
    proxy = load_proxy(monkeypatch, env={"TENANT_ID": "acme", "NAME": "eks-prod"})

    with TestClient(proxy.app) as client:
        monkeypatch.setattr(
            proxy.app.state.client,
            "request",
            raise_on_request(httpx.ConnectTimeout("timed out")),
        )
        response = client.get("/api/v1/namespaces/kube-system")

    assert response.json()["outpost"] == "acme-eks-prod-outpost"


def test_single_client_is_reused_across_requests(monkeypatch):
    proxy = load_proxy(monkeypatch)

    constructed = []

    class CountingClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            constructed.append(self)

    monkeypatch.setattr(proxy.httpx, "AsyncClient", CountingClient)

    calls = []

    async def fake_request(*args, **kwargs):
        calls.append(kwargs["url"])
        return httpx.Response(200, json={"ok": True})

    with TestClient(proxy.app) as client:
        monkeypatch.setattr(proxy.app.state.client, "request", fake_request)

        assert client.get("/api/v1/namespaces/kube-system").status_code == 200
        assert client.get("/api/v1/nodes").json() == {"ok": True}

    # One client for the process, not one per proxied request.
    assert len(constructed) == 1
    assert calls == [
        "https://kubernetes.default.svc/api/v1/namespaces/kube-system",
        "https://kubernetes.default.svc/api/v1/nodes",
    ]


def test_logtee_rejects_non_positive_config(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import logtee

    # Non-positive / unparseable values must fall back to the safe defaults so
    # rotation (and the bounded-log guarantee) can't be silently disabled.
    monkeypatch.setenv("OUTPOST_LOG_MAX_BYTES", "0")
    assert logtee._positive_int_env("OUTPOST_LOG_MAX_BYTES", 5000000) == 5000000

    monkeypatch.setenv("OUTPOST_LOG_BACKUP_COUNT", "-1")
    assert logtee._positive_int_env("OUTPOST_LOG_BACKUP_COUNT", 3) == 3

    monkeypatch.setenv("OUTPOST_LOG_MAX_BYTES", "notanint")
    assert logtee._positive_int_env("OUTPOST_LOG_MAX_BYTES", 5000000) == 5000000

    monkeypatch.setenv("OUTPOST_LOG_MAX_BYTES", "1024")
    assert logtee._positive_int_env("OUTPOST_LOG_MAX_BYTES", 5000000) == 1024


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
