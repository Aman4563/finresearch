"""#479: no code path asks FBIL or RBI for a rate. At run time, every rate consumer runs inside a transport that refuses
any HTTP request at all; statically, no live module imports the FBIL adapter and nothing builds an RBI request."""

from __future__ import annotations

import ast
import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from finresearch.fincalc.dates import IST

SRC = Path(__file__).resolve().parents[1] / "src" / "finresearch"
FIX = Path(__file__).parent / "fixtures"
# the paused FBIL pipeline (#476): the adapter, its backfill/nightly store, and the stored-series reader for the audit
FBIL_ALLOWED = {"adapters/fbil.py", "refseries/rates.py", "refseries/rate_lookup.py"}


@pytest.fixture
def refused(monkeypatch):
    """Every httpx request (sync or async, whatever client or wrapper builds it) is recorded and refused, so a request
    an exception handler swallows is still seen."""
    seen: list[str] = []

    def record(request):
        seen.append(str(request.url))
        raise RuntimeError(f"network refused in this test: {request.url}")

    async def asend(self, request, **kw):
        record(request)

    def send(self, request, **kw):
        record(request)

    monkeypatch.setattr(httpx.AsyncClient, "send", asend)
    monkeypatch.setattr(httpx.Client, "send", send)
    return seen


async def test_every_rate_consumer_answers_with_all_http_refused(monkeypatch, tmp_path, refused):
    from finresearch.adapters import fbil
    from finresearch.adapters.nse_bonds import parse_live_bonds
    from finresearch.monitor import archive as ar
    from finresearch.signals import bond as bond_sig
    from finresearch.signals import fund_rank, rates
    from finresearch.suggest.profile import Profile

    made: list[str] = []

    def no_fbil(*a, **k):  # even a request data_rights would refuse (#476) must not be attempted
        made.append("FbilClient")
        raise AssertionError("FBIL asked")

    monkeypatch.setattr(fbil, "FbilClient", no_fbil)
    listed = parse_live_bonds(json.loads((FIX / "nse" / "bonds_live_trimmed.json").read_text()))

    async def bonds():
        return listed

    async def verified(isin):
        return None

    async def profile():
        return Profile(tax_slab_pct=30)

    monkeypatch.setattr(bond_sig, "SOURCES", bond_sig.BondSources(bonds, None, verified, profile,
                                                                  lambda: date(2026, 9, 28)))  # fmt: skip
    if True:  # every call below runs with all HTTP refused (the `refused` fixture)
        live = await rates.risk_free(30 / 365)
        dated = await rates.rate_for(date(2026, 10, 20), 91 / 365, before=True)
        months, _ = rates.months_for(date(2023, 9, 1), date(2026, 9, 30))
        mar, factsheet_mar, info = fund_rank.mar_for(date(2026, 9, 30))
        signal = await bond_sig.bond_signal("INE413U07475", {"freq": "1"})
        ar.reset_tries()  # the archive's per-day retry state is module-level: other tests leave it behind
        src = ar.ArchiveSources(quote=lambda s: None, fno=None, par_curve=None, watched=lambda: [],
                                near_iv=lambda s, d: None, out_dir=tmp_path)  # fmt: skip
        archived = await ar.archive_step(datetime(2026, 9, 30, 17, 0, tzinfo=IST), src, holidays=set())
    assert refused == [] and made == []  # nothing was even attempted
    assert live["tier"] == "proxy" and live["rate"] is not None and dated["known"]
    assert all(m.y91 is not None for m in months.values()) and factsheet_mar is not None and info["months"]
    assert mar(date(2026, 8, 31), date(2026, 9, 30)) is not None
    assert signal.action == "NO_SIGNAL" and "no G-sec curve source" in signal.caveats[0]
    assert archived["unknown"] == {"gsec_spread": ar.GSEC_UNKNOWN}


@pytest.mark.parametrize("url", ["https://www.fbil.org.in/wasdm/tbill/fetch", "https://www.rbi.org.in/"])
async def test_the_guard_sees_a_request_even_through_the_polite_client(url, refused):
    # the guard above is real: a request through the app's own client is recorded and refused
    from finresearch.adapters.http import PoliteClient

    async def no_wait(_s: float) -> None:
        return None

    c = PoliteClient(cache_dir=None, clock=lambda: 0.0, sleep=no_wait)
    try:
        with pytest.raises((RuntimeError, httpx.HTTPError)):  # the refusal, however the client wraps it
            await c.get(url)
    finally:
        await c.aclose()
    assert refused and refused[0].startswith(url)


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            out |= {node.module, *(f"{node.module}.{a.name}" for a in node.names)}
        elif isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
    return out


def test_no_live_module_imports_the_fbil_adapter():
    users = {str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if "finresearch.adapters.fbil" in _imports(p)}
    assert users <= FBIL_ALLOWED, users - FBIL_ALLOWED


# the rate chain itself: these modules must not even be able to make a request
RATE_MODULES = ("fincalc/policy_rates.py", "fincalc/riskfree.py", "signals/rates.py", "refseries/owner_rates.py",
                "api/rates.py", "fincalc/policy_feed.py", "refseries/policy_obs.py")  # fmt: skip
NETWORK = ("httpx", "finresearch.adapters.http", "finresearch.adapters.fbil", "finresearch.eventcal")


def test_the_rate_chain_imports_no_network_client_and_rbi_rate_collection_is_dropped():
    """The policy rates are typed in by hand or read from RBI's press-release RSS feed by the event calendar's read
    (eventcal.rbi, under its own decision rbi_rss, #481/#492) and stored, and the owner's yields are typed in: no
    module of the chain imports an HTTP client, the FBIL adapter or the event calendar's readers; and the owner's
    decision not to collect RBI rates from RBI's other channels is recorded, so a future collector is refused."""
    from finresearch import data_rights

    for rel in RATE_MODULES:
        bad = {m for m in _imports(SRC / rel) if m.startswith(NETWORK)}
        assert not bad, f"{rel} imports {bad}"
    with pytest.raises(data_rights.NoDecision, match="dropped"):
        data_rights.require("rbi_rates")
