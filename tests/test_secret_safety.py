"""#269: broker secrets (TOTP API key, TOTP seed, access token) can never be read back: not from the API, the database,
the logs (third-party HTTP debug logs included), error messages, sync summaries, the state directory or the market-data
status. A full Groww sync and market-data round run with synthetic secrets and every output is scanned for them."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from finresearch.fincalc.dates import IST

# synthetic values assembled at runtime, so no literal in the repository looks like a credential (gitleaks)
GROWW_KEY = "-".join(["fake", "totp", "key"]) + "x" * 24
SEED = "JBSWY3DP" + "EHPK3PXP"  # the classic example base32 seed (not anyone's)
ACCESS = ".".join(["fake", "access", "y" * 28])
SECRETS = (GROWW_KEY, SEED, ACCESS)
ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}
CSV = ("exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin,underlying_symbol,"
       "underlying_exchange_token,expiry_date,strike_price,lot_size,tick_size,freeze_quantity,is_reserved,"
       "buy_allowed,sell_allowed\nNSE,1001,EXMPL,NSE-EXMPL,Example,EQ,CASH,EQ,INE000A01011,,,,,1,0.05,,0,1,1\n"
       "NSE,1002,EXBANK,NSE-EXBANK,Example Bank,EQ,CASH,EQ,INE000B01012,,,,,1,0.05,,0,1,1\n")  # fmt: skip


def handler(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    if p == "/instruments/instrument.csv":
        return httpx.Response(200, content=CSV.encode())
    if p == "/v1/token/api/access":
        return httpx.Response(200, json={"token": ACCESS, "tokenRefId": "r1", "isActive": True})
    if p == "/v1/holdings/user":
        return httpx.Response(200, json={"status": "SUCCESS", "payload": {"holdings": [
            {"isin": "INE000A01011", "trading_symbol": "EXMPL", "quantity": 10, "average_price": 100,
             "t1_quantity": 0}]}})  # fmt: skip
    if p == "/v1/positions/user":
        return httpx.Response(200, json={"status": "SUCCESS", "payload": {"positions": []}})
    if p == "/v1/order/list":
        return httpx.Response(200, json={"status": "SUCCESS", "payload": {"order_list": []}})
    if p == "/v1/margins/detail/user":
        # a broker error that echoes the caller's credentials back: it must be redacted wherever it is stored or shown
        return httpx.Response(
            500, json={"message": f"internal error for {request.headers.get('authorization')}"}
        )
    if p == "/v1/live-data/ltp":
        return httpx.Response(400, json={"message": f"bad request {request.headers.get('authorization')}"})
    if p == "/v1/historical/candles":
        return httpx.Response(200, json={"status": "SUCCESS", "payload": {"candles": [
            ["2026-10-07T00:00:00", 99, 101, 98, 100.5, 10, None]]}})  # fmt: skip
    return httpx.Response(404, json={"message": f"no route {p}"})


@pytest.fixture
def app_client(env, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.portfolio.connectors import base

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE broker_connection, broker_sync_log, portfolio_disposal, portfolio_lot, "
                           "portfolio_txn, portfolio_holding, portfolio_import, alert CASCADE"))  # fmt: skip

    wipe()
    monkeypatch.setattr(base, "TRANSPORT", httpx.MockTransport(handler))
    with TestClient(create_app(clock=lambda: datetime.now(UTC))) as c:
        yield c
    wipe()


def _scan(where: str, text: str) -> None:
    for s in SECRETS:
        assert s not in text, f"a secret appears in {where}"


def test_sync_and_market_data_never_expose_a_secret(app_client, caplog, monkeypatch, env):
    from sqlalchemy import text

    from finresearch.adapters import groww_budget, groww_market
    from finresearch.db import session_scope
    from finresearch.portfolio.valuation import fetch_prices

    c = app_client
    for name in ("httpx", "httpcore", "finresearch"):  # third-party HTTP debug logs on, at their noisiest
        monkeypatch.setattr(logging.getLogger(name), "level", logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    out: list[str] = []
    r = c.put(
        "/api/connections/groww", headers=ORIGIN, json={"config": {"api_key": GROWW_KEY, "totp_secret": SEED}}
    )
    assert r.status_code == 200
    out.append(r.text)
    sync = c.post("/api/connections/groww/sync", headers=ORIGIN)
    assert (
        sync.json()["status"] == "partial"
    )  # funds failed (HTTP 500 echoing the token): redacted, not fatal
    assert "[redacted]" in sync.json()["error"]
    out += [sync.text, c.get("/api/connections").text, c.get("/api/connections/log?key=groww").text]
    # market data through the real session: the token comes from the store, the LTP error echoes it
    with session_scope() as s:
        exp = s.execute(text("select token_expires_at from broker_connection where key='groww'")).scalar_one()
    clock = exp - timedelta(hours=20)  # 10:00 IST the day the session was granted for
    m = groww_market.GrowwMarket(clock=lambda: clock, holidays=set)
    monkeypatch.setattr(groww_market, "MARKET", m)

    class H:
        id, asset_type, name, nse_symbol, bse_code, isin, scheme_code, meta = (
            1,
            "stock",
            "EXMPL",
            "EXMPL",
            None,
            None,
            None,
            {},
        )

    async def nse(sym, exch="NSE"):
        raise LookupError("offline")

    asyncio.run(fetch_prices([H()], quote=nse, scheme_rows=None))
    assert m.token() == ACCESS  # it really ran with the stored token
    out.append(c.get("/api/connections/groww/market-data").text)
    for i, t in enumerate(out):
        _scan(f"API response {i}", t)
    # database rows: only references
    with session_scope() as s:
        for table in ("broker_connection", "broker_sync_log", "alert", "portfolio_import", "portfolio_txn"):
            rows = s.execute(text(f"select row_to_json(t)::text from {table} t")).scalars().all()
            _scan(f"table {table}", "\n".join(rows))
        cfg = s.execute(text("select config::text, token from broker_connection where key='groww'")).one()
        assert "secret_ref" in cfg[0] and cfg[1].startswith("secret_ref:")
    # logs, with httpx/httpcore at DEBUG
    _scan("the logs", caplog.text + "\n".join(str(r.exc_text or "") for r in caplog.records))
    # the state directory (rate-limit counters, Groww caches, instruments file)
    for f in Path(env.state_dir).rglob("*"):
        if f.is_file() and f.name != "secrets.json":
            _scan(f"state file {f.name}", f.read_text(errors="replace"))
    usage = {row["category"]: row["calls"] for row in groww_budget.BUDGET.today()["categories"]}
    assert usage["auth"] == 1 and usage["non_trading"] == 4 and usage["instruments"] == 1


def test_log_records_are_redacted_whatever_the_handler(caplog):
    from finresearch import logredact

    logredact.register(
        ACCESS, "123456"
    )  # a 6-digit code is too short to register (it would blank other numbers)
    caplog.set_level(logging.DEBUG)
    log = logging.getLogger("httpcore.http11")
    log.debug("send_request_headers.started request=%r", [(b"Authorization", f"Bearer {ACCESS}".encode())])
    log.info("totp_secret=%s api_key: %s", SEED, "k" * 12)
    try:
        raise RuntimeError(f"token {ACCESS} refused")
    except RuntimeError:
        logging.getLogger("finresearch.x").exception("sync failed")
    text = caplog.text + "\n".join(str(r.exc_text or "") for r in caplog.records)
    assert ACCESS not in text and SEED not in text and "k" * 12 not in text
    assert "[redacted]" in text and "123456" not in logredact._values


def test_noisy_http_loggers_are_quiet_by_default():
    import finresearch  # noqa: F401  (installs the redaction)
    from finresearch import logredact

    for name in logredact.QUIET:
        assert logging.getLogger(name).level == logging.WARNING, name


def test_prompt_building_code_never_imports_the_secret_store():
    """Broker credentials can only reach a model if a prompt-building module can read them: none imports the secret
    store, the connection store or the Groww market client."""
    root = Path(__file__).resolve().parents[1] / "src" / "finresearch"
    banned = ("finresearch.secrets", "from finresearch import secrets", "connectors.store", "groww_market",
              "connectors.sync")  # fmt: skip
    for pkg in ("agents", "bridge", "orchestrator", "suggest", "mcp_server", "verify"):
        for f in (root / pkg).rglob("*.py"):
            src = f.read_text()
            for b in banned:
                assert b not in src, f"{f.relative_to(root)} imports {b}"


def test_keychain_values_never_travel_in_argv():
    """The Keychain backend writes through `security -i` (stdin) and reads with `-w` to a pipe: no secret in argv."""
    from finresearch.secrets import KeychainBackend

    calls = []

    class P:
        returncode, stdout = 0, ""

    def run(argv, **kw):
        calls.append((argv, kw.get("input")))
        p = P()
        p.stdout = "b64:" + __import__("base64").b64encode(ACCESS.encode()).decode() if "-w" in argv else ""
        return p

    kc = KeychainBackend(run=run)
    kc.set("finresearch/db/broker.groww/token", ACCESS)
    assert all(ACCESS not in " ".join(a) for a, _ in calls)
    assert any(i and "b64:" in i for _, i in calls)  # the value went on stdin, base64-encoded


@pytest.mark.parametrize("value", [GROWW_KEY, ACCESS])
def test_market_data_status_has_no_secret(value, monkeypatch):
    from finresearch.adapters import groww_market

    class S:
        state = "connected"

        def token(self):
            return value

        def forget(self):
            pass

    m = groww_market.GrowwMarket(
        session=S(), clock=lambda: datetime(2026, 10, 9, 10, tzinfo=IST), holidays=set
    )
    assert value not in json.dumps(m.status())
