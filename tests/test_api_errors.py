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
