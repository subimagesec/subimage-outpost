import sys
from pathlib import Path


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
