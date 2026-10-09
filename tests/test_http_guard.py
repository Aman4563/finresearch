"""The shared HTTP client refuses URLs aimed at this machine or a private network (SSRF defence in depth)."""

from __future__ import annotations

import asyncio

import pytest

from finresearch.adapters.http import PoliteClient, UnsafeURLError, check_public_url


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8710/api/profile", "http://localhost/x", "http://api.localhost/x", "http://10.0.0.5/x",
     "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/x", "http://0.0.0.0/x",
     "file:///etc/passwd", "ftp://example.com/x", "http:///nohost"],
)  # fmt: skip
def test_unsafe_urls_are_refused(url):
    with pytest.raises(UnsafeURLError):
        check_public_url(url)


@pytest.mark.parametrize(
    "url", ["https://www.nseindia.com/api/quote-equity?symbol=INFY", "https://8.8.8.8/x"]
)
def test_public_urls_pass(url):
    assert check_public_url(url) == url


def test_the_client_refuses_before_any_request():
    async def go():
        async with PoliteClient(cache_dir=None) as c:
            await c.get("http://169.254.169.254/latest/meta-data/")

    with pytest.raises(UnsafeURLError):
        asyncio.run(go())


def test_oauth_callback_redirects_only_to_the_local_app():
    from finresearch.api.connections import LOCAL_APP_DEFAULT, _local_app_url

    assert _local_app_url("http://127.0.0.1:3100") == "http://127.0.0.1:3100"
    assert _local_app_url("http://localhost:3005/") == "http://localhost:3005"
    for bad in (
        "https://evil.example",
        "http://user:pw@127.0.0.1:3100",
        "javascript:alert(1)",
        "",
        "http://10.0.0.2",
    ):
        assert _local_app_url(bad) == LOCAL_APP_DEFAULT


def test_lookthrough_store_rejects_ids_that_are_not_its_own(tmp_path):
    from finresearch.portfolio.lookthrough import PortfolioStore

    st = PortfolioStore(tmp_path)
    (tmp_path / "parsed").mkdir()
    (tmp_path / "parsed" / "victim.json").write_text("{}")
    assert st.delete_file("../parsed/victim") is False and st.parsed("../parsed/victim") == []
    assert (tmp_path / "parsed" / "victim.json").exists()
