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


# --------------------------------------------------------------------------- agent-chosen URLs resolve first (#259)
def _dns(table):
    """A fake getaddrinfo: host -> addresses (no real DNS in tests); an unknown host is NXDOMAIN."""
    import socket

    def resolve(host, port):
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
                for a in table[host]]  # fmt: skip

    return resolve


@pytest.mark.parametrize(
    "addr",
    ["10.1.2.3", "172.16.0.9", "192.168.0.10", "127.0.0.1", "169.254.169.254", "100.64.0.1", "100.127.255.254",
     "0.0.0.0", "224.0.0.251", "240.0.0.1", "255.255.255.255", "192.0.2.10", "::1", "::", "fe80::1", "fc00::5",
     "ff02::1", "::ffff:10.0.0.1", "::ffff:127.0.0.1", "2002:a00:1::1", "64:ff9b::a9fe:a9fe"],
)  # fmt: skip
def test_a_name_resolving_to_a_non_public_address_is_refused(addr):
    with pytest.raises(UnsafeURLError, match="private, loopback or reserved"):
        check_public_url("https://news.example.com/a", resolve=_dns({"news.example.com": [addr]}))


def test_every_address_must_be_public_and_an_unresolvable_name_is_refused():
    mixed = _dns({"news.example.com": ["93.184.216.34", "10.0.0.7"]})
    with pytest.raises(UnsafeURLError):
        check_public_url("https://news.example.com/a", resolve=mixed)
    with pytest.raises(UnsafeURLError, match="could not resolve"):
        check_public_url("https://nowhere.example.com/a", resolve=_dns({}))
    with pytest.raises(UnsafeURLError, match="local network name"):
        check_public_url("http://router.local/", resolve=_dns({"router.local": ["93.184.216.34"]}))
    ok = _dns({"news.example.com": ["93.184.216.34", "2606:2800:220:1::1", "::ffff:8.8.8.8"]})
    assert check_public_url("https://news.example.com/a", resolve=ok) == "https://news.example.com/a"
    # literal addresses never need DNS; names are not resolved without `resolve` (fixed exchange hosts)
    assert check_public_url("https://8.8.8.8/x", resolve=_dns({})) == "https://8.8.8.8/x"
    assert check_public_url("https://private-only.example.com/x") == "https://private-only.example.com/x"


class _NoSlots:
    def reserve(self, key, interval):
        return 0.0

    def hold_off(self, key, seconds):
        return None


def _fetch(handler, table, **kw):
    import httpx

    from finresearch.adapters.http import HostRateLimiter, fetch_public

    seen: list[str] = []

    def record(request):
        seen.append(str(request.url))
        return handler(request)

    async def go():
        return await fetch_public(kw.pop("url", "https://news.example.com/a"), resolve=_dns(table),
                                  transport=httpx.MockTransport(record),
                                  limiter=HostRateLimiter(slots=_NoSlots()), **kw)  # fmt: skip

    return asyncio.run(go()), seen


PUBLIC = {
    "news.example.com": ["93.184.216.34"],
    "cdn.example.net": ["151.101.1.1"],
    "evil.example.org": ["10.0.0.5"],
}


def test_fetch_public_follows_public_redirects_and_refuses_a_private_hop():
    import httpx

    def handler(request):
        if request.url.host == "news.example.com":
            return httpx.Response(302, headers={"location": "https://cdn.example.net/page"})
        if request.url.host == "cdn.example.net" and request.url.path == "/page":
            return httpx.Response(
                200, text="<p>Revenue ₹1,234 crore</p>", headers={"content-type": "text/html"}
            )
        if request.url.host == "cdn.example.net":
            return httpx.Response(301, headers={"location": "https://evil.example.org/latest/meta-data"})
        raise AssertionError(f"a refused hop was requested: {request.url}")

    got, seen = _fetch(handler, PUBLIC)
    assert got.ok and "Revenue" in got.text and got.record.url == "https://cdn.example.net/page"
    assert seen == ["https://news.example.com/a", "https://cdn.example.net/page"]
    with pytest.raises(UnsafeURLError, match=r"10\.0\.0\.5"):
        _fetch(handler, PUBLIC, url="https://cdn.example.net/moved")
    with pytest.raises(UnsafeURLError):  # a redirect straight to a literal loopback address
        _fetch(
            lambda r: httpx.Response(302, headers={"location": "http://127.0.0.1:8710/api/portfolio"}), PUBLIC
        )


def test_fetch_public_stops_after_five_redirects():
    import httpx

    def loop(request):
        n = int(request.url.params.get("n", "0"))
        return httpx.Response(302, headers={"location": f"/a?n={n + 1}"})

    with pytest.raises(UnsafeURLError, match="more than 5 redirects"):
        _fetch(loop, PUBLIC)


def test_fetch_public_caps_the_body_declared_or_streamed():
    import httpx

    from finresearch.adapters.http import ResponseTooLarge

    big = b"x" * 2048
    with pytest.raises(
        ResponseTooLarge, match="over the 1,000-byte cap"
    ):  # Content-Length says so: never read
        _fetch(lambda r: httpx.Response(200, content=big), PUBLIC, max_bytes=1000)

    def chunked(request):  # no Content-Length: aborted while streaming
        async def body():
            for _ in range(10):
                yield b"y" * 300

        return httpx.Response(200, content=body())

    with pytest.raises(ResponseTooLarge, match="passed the 1,000-byte cap"):
        _fetch(chunked, PUBLIC, max_bytes=1000)
    got, _ = _fetch(lambda r: httpx.Response(200, content=b"z" * 1000), PUBLIC, max_bytes=1000)
    assert got.record.size == 1000  # exactly at the cap is allowed


def test_fetch_page_resolves_the_host_before_any_request(monkeypatch):
    """The fetch_page path (verify.web.fetch without a test FETCHER) refuses a public-looking name that resolves to a
    private address, before any connection: the DNS is faked and no transport exists, so a request would fail
    differently."""
    import socket

    from finresearch.verify import web

    monkeypatch.setattr(web, "FETCHER", None)
    monkeypatch.setattr(socket, "getaddrinfo", _dns({"intranet.example.com": ["192.168.1.20"]}))
    with pytest.raises(UnsafeURLError, match=r"192\.168\.1\.20"):
        asyncio.run(web.fetch("https://intranet.example.com/hr/payroll"))
