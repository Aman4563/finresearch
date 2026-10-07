"""#246c: the local API token. Every route but /api/health answers 401 without it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from finresearch import config

TOKEN = "synthetic-api-token-for-tests"
NO_AUTH = {"Authorization": ""}  # overrides the conftest default bearer header
DASHBOARD = "http://127.0.0.1:3100"


@pytest.fixture
def app(env):
    from finresearch.api import create_app

    return create_app(api_token=TOKEN)


def test_routes_need_the_token_except_health(app):
    anon = TestClient(app, headers=NO_AUTH)
    assert anon.get("/api/health").status_code == 200
    for path in ("/api/companies", "/api/export/all.zip", "/api/connections", "/api/portfolio", "/api/docs"):
        r = anon.get(path)
        assert r.status_code == 401, path
        assert r.headers["www-authenticate"] == "Bearer"
    assert anon.post("/api/watches", json={}).status_code == 401
    wrong = TestClient(app, headers={"Authorization": "Bearer not-the-token"})
    assert wrong.get("/api/companies").status_code == 401
    ok = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.get("/api/companies").status_code == 200
    z = ok.get("/api/export/all.zip")
    assert z.status_code == 200 and z.content[:2] == b"PK"


def test_the_app_cookie_is_accepted_and_scoped_by_api_port(app):
    # TestClient's server is testserver:80, so the cookie the web app would set is finresearch_token_80
    c = TestClient(app, headers=NO_AUTH, cookies={"finresearch_token_80": TOKEN})
    assert c.get("/api/companies").status_code == 200
    other_port = TestClient(app, headers=NO_AUTH, cookies={"finresearch_token_8710": TOKEN})
    assert other_port.get("/api/companies").status_code == 401
    bad = TestClient(app, headers=NO_AUTH, cookies={"finresearch_token_80": TOKEN + "x"})
    assert bad.get("/api/companies").status_code == 401


def test_cors_preflight_and_401_carry_cors_headers_with_credentials(app):
    anon = TestClient(app, headers=NO_AUTH)
    pre = anon.options(
        "/api/companies", headers={"Origin": DASHBOARD, "Access-Control-Request-Method": "GET"}
    )
    assert pre.status_code == 200 and pre.headers["access-control-allow-credentials"] == "true"
    r = anon.get("/api/companies", headers={"Origin": DASHBOARD})
    assert r.status_code == 401 and r.headers["access-control-allow-origin"] == DASHBOARD


def test_broker_oauth_callback_is_reachable_without_the_cookie(app):
    # the broker redirects the browser here cross-site, where a SameSite=Strict cookie is not sent; the route checks
    # its own one-time state instead
    r = TestClient(app, headers=NO_AUTH, follow_redirects=False).get(
        "/api/connections/zerodha/callback?code=x"
    )
    assert r.status_code == 303 and "connect_error" in r.headers["location"]
    assert TestClient(app, headers=NO_AUTH).delete("/api/connections/zerodha/callback").status_code == 401


def test_create_app_refuses_to_start_without_a_token(env, monkeypatch):
    from finresearch.api import create_app

    monkeypatch.delenv("FINRESEARCH_API_TOKEN")
    config.get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="no local API token"):
        create_app()


def test_bootstrap_keeps_one_token_in_the_store_and_a_private_file(monkeypatch):
    from finresearch import secrets as secret_store
    from finresearch.api import auth

    monkeypatch.delenv("FINRESEARCH_API_TOKEN")
    config.get_settings.cache_clear()
    tok = auth.bootstrap()
    assert len(tok) >= 40
    f = auth.token_file()
    assert f.read_text() == tok and f.stat().st_mode & 0o777 == 0o600
    assert secret_store.backend().get(secret_store.ref_for("api", "token")) == tok
    f.unlink()
    assert auth.bootstrap() == tok and f.read_text() == tok  # the Keychain copy restores the file
    assert auth.client_headers() == {"Authorization": f"Bearer {tok}"}


def test_serve_bootstraps_the_token_and_starts_a_guarded_app(env, monkeypatch):
    import uvicorn
    from typer.testing import CliRunner

    from finresearch.api import auth
    from finresearch.cli import app as cli

    monkeypatch.delenv("FINRESEARCH_API_TOKEN")
    config.get_settings.cache_clear()
    started = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.update(app=app, **kw))
    out = CliRunner().invoke(cli, ["serve", "--no-monitor", "--port", "8799"])
    assert out.exit_code == 0, out.output
    assert started["host"] == "127.0.0.1" and started["port"] == 8799
    tok = auth.token_file().read_text()
    assert TestClient(started["app"], headers=NO_AUTH).get("/api/companies").status_code == 401
    good = TestClient(started["app"], headers={"Authorization": f"Bearer {tok}"})
    assert good.get("/api/companies").status_code == 200
