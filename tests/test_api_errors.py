"""Error text shown by the API: class and message, with secrets and local paths removed (CodeQL py/stack-trace-exposure)."""

from __future__ import annotations

from finresearch.api.errors import public_error, redact

# made up, and assembled at runtime so secret scanners don't read the fixture as a real token
FAKE_BOT_TOKEN = "123456789" + ":" + "AAH" + "x" * 32


def test_credentials_tokens_and_paths_are_removed():
    s = redact("connection to postgresql+psycopg://alice:s3cret@db:5432/x failed; "
               f"GET https://api.telegram.org/bot{FAKE_BOT_TOKEN}/sendMessage; "
               "https://x.example/api?symbol=INFY&api_key=abcdef123&token=zzz; Authorization: Bearer abcdefghijklmnopqrstu; "
               "file /Users/someone/finresearch/data/portfolio/x.pdf and C:\\Users\\someone\\x.pdf")  # fmt: skip
    for leaked in ("alice", "s3cret", "123456789:AAH", "abcdef123", "zzz", "abcdefghijklmnopqrstu", "/Users/someone",
                   "someone\\x.pdf"):  # fmt: skip
        assert leaked not in s, leaked
    assert "symbol=INFY" in s and "postgresql+psycopg://***@db:5432" in s


def test_public_error_keeps_the_class_and_a_bounded_message():
    e = RuntimeError("NSE HTTP 503 for /api/quote-equity" + "x" * 1000)
    out = public_error(e, 120)
    assert out.startswith("RuntimeError: NSE HTTP 503") and len(out) == 120
    assert public_error(ValueError()) == "ValueError"


def test_xirr_reason_is_fixed_text_never_the_exception():
    """Code scanning #28: the XIRR reason reaches the API, so it is our own fixed wording."""
    from datetime import date
    from decimal import Decimal

    from finresearch.portfolio.valuation import xirr_or_reason

    today = date(2026, 10, 9)
    _, why = xirr_or_reason([(date(2025, 1, 1), Decimal(100)), (today, Decimal(50))], today)
    assert why == "needs at least one investment and one current value"
    _, why = xirr_or_reason([(date(2025, 1, 1), Decimal(-1)), (today, Decimal("1e12"))], today)
    assert why == "no XIRR between -99.99 % and 1000 %"


# --------------------------------------------------------------------------- the API's generic 422 and 502 (#261)
SECRET_PATH = "/Users/someone/finresearch/data/portfolio/cas_2026.pdf"
SECRET_URL = "https://upstream.example/api/report?session=sess-9f8e7d6c&pan=ABCDE1234F&token=tok-1a2b3c"
SECRET_DSN = "postgresql+psycopg://alice:s3cret-pw@db.example:5432/finresearch"
SECRETS = (
    "/Users/someone",
    "cas_2026.pdf",
    "sess-9f8e7d6c",
    "ABCDE1234F",
    "tok-1a2b3c",
    "alice",
    "s3cret-pw",
)


def _app_with_failing_routes():
    import httpx

    from finresearch.adapters.nse import NseError
    from finresearch.api import create_app

    app = create_app()
    msg = f"could not read {SECRET_PATH} via {SECRET_URL} ({SECRET_DSN})"

    @app.get("/api/_test/value-error")
    def value_error():
        raise ValueError(msg)

    @app.get("/api/_test/httpx-error")
    def httpx_error():
        raise httpx.ConnectError(msg, request=httpx.Request("GET", SECRET_URL))

    @app.get("/api/_test/nse-error")
    def nse_error():
        raise NseError(msg)

    return app


def test_generic_422_and_502_never_return_paths_urls_or_credentials(env, caplog):
    import logging

    from fastapi.testclient import TestClient

    with (
        caplog.at_level(logging.WARNING, logger="finresearch.api"),
        TestClient(_app_with_failing_routes()) as c,
    ):
        got = {p: c.get(f"/api/_test/{p}") for p in ("value-error", "httpx-error", "nse-error")}
    assert got["value-error"].status_code == 422
    assert got["httpx-error"].status_code == 502 and got["nse-error"].status_code == 502
    for path, r in got.items():
        detail = r.json()["detail"]
        for leaked in SECRETS:
            assert leaked not in r.text, (path, leaked)
        # still says what failed: the exception class and the host, without the query or the file
        assert "upstream.example/api/report?<query>" in detail and "<path>" in detail, detail
    assert got["value-error"].json()["detail"].startswith("ValueError: could not read")
    assert got["httpx-error"].json()["detail"].startswith("upstream source failed: ConnectError:")
    # the full detail stays in the server log
    logged = "\n".join(rec.getMessage() + str(rec.exc_info and rec.exc_info[1]) for rec in caplog.records)
    assert "sess-9f8e7d6c" in logged and SECRET_PATH in logged


def test_drop_query_cuts_only_the_query_string():
    e = RuntimeError("HTTP 500 for https://x.example/a/b?symbol=INFY&sid=abc#frag and http://y.example/c")
    assert public_error(e, drop_query=True) == (
        "RuntimeError: HTTP 500 for https://x.example/a/b?<query>#frag and http://y.example/c"
    )
    assert "sid=abc" in public_error(
        e
    )  # the default keeps non-secret parameters (redact still masks token=...)
