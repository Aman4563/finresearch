"""The F&O book's scenario view (#394, fnobook.scenarios and GET /api/fno/scenarios): today's paper and Groww legs
priced on a synthetic option chain, then exposures, the revaluation grid, replays and reverse stress. Synthetic
contracts and prices only; no network."""

from __future__ import annotations

import math
from decimal import Decimal as D
from statistics import NormalDist

import pytest
from test_fno_book import (  # noqa: F401
    EXPIRY,
    FNO_ROWS,
    TRADE_DAY,
    FakeMarket,
    _record_spread,
    _store_groww_positions,
    at_ist,
    book_db,
    offline,
    run,
)

N = NormalDist()
pytestmark = pytest.mark.usefixtures("book_db")
NOW = at_ist(TRADE_DAY, 11)  # 12-Oct-2026 11:00 IST
T = (15 * 1440 + 270) / 525_600  # to 27-Oct 15:30 IST, to the minute (#359)
# #479, #480: each expiry is priced at signals.rates' rate at its own tenor: the RBI repo-rate proxy, 5.50 % since the
# 07-Oct-2026 hike (conftest pins "today" at 23-Oct-2026), a simple Actual/365 yield over the expiry's 15 days, so
# z = ln(1 + 0.055 x 15/365) x 365/15 continuous; no FBIL curve and no fixed 6.5 %
R = math.log(1 + 0.055 * 15 / 365) * 365 / 15
F = 24980.0  # the synthetic forward the chain's prices are built on
SPOT = 24900.0  # the chain's underlying
# #480: the carry the scenarios roll the forward at, from the parity fit and the chain's underlying:
# ln(24,980 / 24,900) / T = 7.71 % a year (the old rule rolled it at R)
B = math.log(F / SPOT) / T


def _b76(right, f, k, t, v, r=R):
    d1 = (math.log(f / k) + v * v * t / 2) / (v * math.sqrt(t))
    c = math.exp(-r * t) * (f * N.cdf(d1) - k * N.cdf(d1 - v * math.sqrt(t)))
    return c if right == "call" else c - math.exp(-r * t) * (f - k)


VOLS = {24800: 0.145, 25000: 0.14, 25200: 0.135}


def chain(drop=(), ltp_only=(), *, expiry=EXPIRY, f=F, t=T, r=R, strikes=None):
    """A chain whose mids are Black-76 prices on `f` at VOLS (bid/ask ±0.5 around them); `drop` removes quotes and
    `ltp_only` leaves a quote with a last price but no bid/ask. Three strikes, so the parity fit (#480) can read the
    carry; `strikes` keeps only some."""
    from finresearch.adapters.nse_fno import ChainRow, OptionChain, OptionQuote

    def q(right, k):
        if (right, k) in drop:
            return None
        mid = _b76(right, f, k, t, VOLS[k], r)
        two = (right, k) not in ltp_only
        return OptionQuote(last_price=D(str(round(mid, 2))), iv=D("99"), oi=None, change_in_oi=None, volume=None,
                           bid=D(str(mid - 0.5)) if two else None, ask=D(str(mid + 0.5)) if two else None)  # fmt: skip

    return OptionChain(symbol="NIFTY", expiry=expiry, underlying=D(str(SPOT)), as_of=None,
                       rows=[ChainRow(strike=D(k), call=q("call", k), put=q("put", k))
                             for k in (VOLS if strikes is None else strikes)])  # fmt: skip


@pytest.fixture(autouse=True)
def groww_market(monkeypatch):
    from finresearch.adapters import groww_market

    monkeypatch.setattr(groww_market, "MARKET", FakeMarket())


class Sources:
    def __init__(self, ch):
        self.ch, self.calls = ch, []

    async def chain(self, symbol, expiry):
        self.calls.append((symbol, expiry))
        ch = self.ch.get(expiry) if isinstance(self.ch, dict) else self.ch
        if ch is None:
            raise RuntimeError("no chain")
        return ch


def _paper_spread():
    from finresearch.db import session_scope

    with session_scope() as s:
        _record_spread(s, NOW)  # long 75 x 25000 CE, short 75 x 25200 CE, expiry 27-Oct-2026


def test_paper_legs_are_priced_at_mids_on_the_parity_forward():
    from finresearch.fnobook import scenarios

    _paper_spread()
    v = run(scenarios.view(NOW, sources=Sources(chain()), capital=500_000))
    (fw,) = v["forwards"]
    assert fw["rate"] == pytest.approx(R) and fw["rate_info"]["tenor_days"] == 15
    assert fw["rate_info"]["as_of"] == "2026-10-07" and fw["rate_info"]["source"].startswith(
        "RBI repo-rate proxy"
    )
    assert (
        fw["forward"] == pytest.approx(F, abs=1e-6) and "parity fit" in fw["source"]
    )  # mids reproduce F exactly
    # #480: the carry its scenarios roll it at, ln(24,980 / 24,900) / T, and the dividend yield r - b it implies
    assert (
        fw["spot"] == SPOT
        and fw["carry"] == pytest.approx(B, rel=1e-9)
        and fw["carry"] == pytest.approx(0.0771, abs=1e-4)
    )
    assert "parity fit over 3 strikes" in fw["carry_source"] and fw["carry_reason"] is None
    assert fw["implied_q"] == pytest.approx(R - B, rel=1e-9)
    legs = {x["strike"]: x for x in v["legs"]}
    assert legs[25000]["mark_kind"] == "mid" and legs[25000]["iv"] == pytest.approx(0.14, abs=1e-6)
    assert legs[25200]["qty"] == -75 and legs[25200]["iv"] == pytest.approx(0.135, abs=1e-6)
    assert v["exposures"]["complete"] and v["grid"]["complete"]
    assert len(v["grid"]["cells"]) == 14 * 5 * 3
    assert {r["horizon"] for r in v["reverse"]["rows"]} == {"1d", "5d", "expiry"}
    assert v["reverse"]["limit"] == pytest.approx(40_000)  # 8 % of 5 lakh (#358)
    assert any("Groww is not connected" in n for n in v["notes"])
    # a debit spread loses at most its debit: no grid cell loses more than (C25000 - C25200) x 75
    debit = (_b76("call", F, 25000, T, 0.14) - _b76("call", F, 25200, T, 0.135)) * 75
    assert min(c["pnl"] for c in v["grid"]["cells"]) >= -debit - 0.005  # cells are rounded to the paisa


def test_a_missing_quote_makes_the_book_incomplete_never_zero():
    from finresearch.fnobook import scenarios

    _paper_spread()
    v = run(scenarios.view(NOW, sources=Sources(chain(drop={("call", 25200)})), capital=500_000))
    assert not v["exposures"]["complete"] and v["exposures"]["total"] is None
    assert not v["grid"]["complete"] and all(c["pnl"] is None for c in v["grid"]["cells"])
    assert any("25200" in r for r in v["grid"]["reasons"])
    assert all(r["cell"] is None and not r["complete"] for r in v["reverse"]["rows"])
    assert all(r["pnl"] is None for r in v["replays"])


def test_an_ltp_only_quote_is_flagged_and_no_chain_means_no_forward():
    from finresearch.fnobook import scenarios

    _paper_spread()
    v = run(scenarios.view(NOW, sources=Sources(chain(ltp_only={("call", 25200)})),
                           capital=500_000))  # fmt: skip
    assert {x["strike"]: x["mark_kind"] for x in v["legs"]} == {25000: "mid", 25200: "ltp"}
    assert any("LTP" in f for f in v["exposures"]["flags"])
    v = run(scenarios.view(NOW, sources=Sources(None), capital=500_000))
    assert not v["grid"]["complete"] and any("forward" in r for r in v["grid"]["reasons"])


def test_no_capital_means_no_book_limit():
    from finresearch.fnobook import scenarios

    _paper_spread()
    v = run(scenarios.view(NOW, sources=Sources(chain()), capital=0))
    assert v["reverse"]["limit"] is None and "capital" in v["reverse"]["reason"]
    assert v["reverse"]["rows"] == []


def test_an_empty_book_is_complete_and_flat():
    from finresearch.fnobook import scenarios

    src = Sources(chain())
    v = run(scenarios.view(NOW, sources=src, capital=500_000))
    assert v["legs"] == [] and v["grid"]["complete"] and all(c["pnl"] == 0 for c in v["grid"]["cells"])
    assert src.calls == []  # nothing open: no chain is read


def test_groww_legs_join_the_book():
    from finresearch.fnobook import scenarios

    _store_groww_positions([FNO_ROWS[0]], NOW)  # long 75 x 25000 CE, direction confirmed
    v = run(scenarios.view(NOW, sources=Sources(chain()), capital=500_000))
    (leg,) = v["legs"]
    assert (leg["source"], leg["qty"], leg["strike"], leg["right"]) == ("groww", 75, 25000, "call")
    assert v["grid"]["complete"] and v["exposures"]["complete"]


def test_unresolved_groww_positions_make_every_figure_unknown():
    from finresearch.fnobook import scenarios

    _paper_spread()
    _store_groww_positions(FNO_ROWS, NOW)  # one row's long/short is not confirmed
    v = run(scenarios.view(NOW, sources=Sources(chain()), capital=500_000))
    assert not v["grid"]["complete"] and all(c["pnl"] is None for c in v["grid"]["cells"])
    assert v["exposures"]["total"] is None and not v["exposures"]["complete"]
    assert all(r["pnl"] is None for r in v["replays"]) and all(
        not r["complete"] for r in v["reverse"]["rows"]
    )
    assert any("could not be resolved" in r for r in v["grid"]["reasons"])


def _profile_rows():
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import InvestorProfile, PaperStrategy

    with session_scope() as s:
        return ([(p.name, p.data) for p in s.scalars(select(InvestorProfile).order_by(InvestorProfile.name))],
                [(p.mtm_gross, p.mtm_at) for p in s.scalars(select(PaperStrategy))])  # fmt: skip


@pytest.mark.parametrize("capital", [None, 500_000])
def test_api_route_reads_the_profile_capital_and_writes_nothing(monkeypatch, capital):
    """The profile is set here, never inherited from another test's rows (other test files leave one behind): none
    stored (capital unknown: no limit) or one with F&O capital. The GET must leave profile and paper rows as they were."""
    from sqlalchemy import delete
    from starlette.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.db.models import InvestorProfile
    from finresearch.fnobook import sources
    from finresearch.suggest.advisor import default_profile, save_profile

    with session_scope() as s:
        s.execute(delete(InvestorProfile))
        if capital is not None:
            save_profile(s, default_profile().model_copy(update={"fno_capital_inr": D(capital)}))
    _paper_spread()
    before = _profile_rows()
    monkeypatch.setattr(sources, "SOURCES", Sources(chain()))
    r = TestClient(create_app(clock=lambda: NOW)).get("/api/fno/scenarios")
    assert r.status_code == 200
    v = r.json()
    assert v["grid"]["complete"] and len(v["legs"]) == 2
    if capital is None:
        assert v["reverse"]["limit"] is None and "capital" in v["reverse"]["reason"]
    else:
        assert v["reverse"]["limit"] == pytest.approx(40_000) and len(v["reverse"]["rows"]) == 3  # 8 % (#358)
    assert (
        _profile_rows() == before
    )  # the GET wrote nothing: no profile row created or changed, marks untouched
    assert len(before[0]) == (0 if capital is None else 1)


def test_a_groww_future_shows_no_strike(monkeypatch):
    from finresearch.fincalc import options as o
    from finresearch.fnobook import scenarios
    from finresearch.signals import tradeplan

    async def real(today):  # real_positions carries a future's average price in Leg.strike
        return [tradeplan.RealPosition("NIFTY", EXPIRY, [o.Leg("future", 24950.0, 0.0, -75)])], None

    monkeypatch.setattr(tradeplan, "real_positions", real)
    legs, _ = run(scenarios.groww_legs(TRADE_DAY))
    assert [(x.right, x.strike, x.qty) for x in legs] == [("future", 0.0, -75)]


def test_the_joint_risk_book_prices_at_the_repo_proxy_and_names_it():
    """#366/#385, #479: the joint risk view's book (portfolio.joint_risk.book) uses the same rate as the scenarios
    page (the RBI repo-rate proxy at the expiry's tenor) and says which, in its notes."""
    from finresearch.portfolio import joint_risk as jl

    _paper_spread()
    b, _, notes = run(jl.book(NOW, sources=Sources(chain())))
    assert b.market.rate_for(EXPIRY) == pytest.approx(R)
    assert any(
        n.startswith("rate: RBI repo-rate proxy: repo 5.50 % in force from 07-Oct-2026") for n in notes
    )
    # #480: and rolls its forward at the expiry's carry, named in a note
    assert b.market.carry("NIFTY", EXPIRY).b == pytest.approx(B, rel=1e-9)
    assert any(n.startswith("carry: NIFTY 27-Oct-2026: 7.71% a year (put-call parity fit") for n in notes)


# --------------------------------------------------------------------------- #480: the carry and rate per expiry
@pytest.fixture
def no_bhav(env):
    """No stored bhavcopy rows before or after (other test files leave futures behind)."""
    from sqlalchemy import text

    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE fno_bhav"))

    wipe()
    yield
    wipe()


def _future(day, settle, underlying):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav

    with session_scope() as s:
        s.add(FnoBhav(day=day, symbol="NIFTY", instrument="IDF", expiry=EXPIRY, strike=D(0), option_type="XX",
                      settle=D(str(settle)), close=D(str(settle)),
                      underlying=None if underlying is None else D(str(underlying))))  # fmt: skip


def test_without_a_reliable_parity_fit_the_carry_is_the_stored_futures_settle_else_unknown(no_bhav):
    from datetime import date

    from finresearch.fnobook import scenarios

    _paper_spread()
    two = Sources(chain(strikes=(25000, 25200)))  # two strikes: no parity fit (it needs three)
    v = run(scenarios.view(NOW, sources=two, capital=500_000))
    (fw,) = v["forwards"]
    assert fw["forward"] == pytest.approx(F, abs=1e-6)  # still priced, on the single-strike parity forward
    assert v["exposures"]["complete"] and fw["carry"] is None and "parity" in fw["carry_reason"]
    assert not v["grid"]["complete"] and all(c["pnl"] is None for c in v["grid"]["cells"])
    assert all(r["pnl"] is None for r in v["replays"])
    assert any("carry" in r and "never rolled at the risk-free rate" in r for r in v["grid"]["reasons"])
    # a stored future 11 days old is not used (an ex-date or a rate decision may lie in between)
    _future(date(2026, 10, 1), 25000.0, 24890.0)
    v = run(scenarios.view(NOW, sources=two, capital=500_000))
    assert v["forwards"][0]["carry"] is None and "01-Oct-2026" in v["forwards"][0]["carry_reason"]
    # the latest stored same-expiry future (Fri 9-Oct, 18 days to expiry) against the close stored with it
    _future(date(2026, 10, 9), 25010.0, 24900.0)
    v = run(scenarios.view(NOW, sources=two, capital=500_000))
    (fw,) = v["forwards"]
    assert fw["carry"] == pytest.approx(math.log(25010 / 24900) / (18 / 365), rel=1e-12)  # 8.94 % a year
    assert "future's settle" in fw["carry_source"] and "09-Oct-2026" in fw["carry_source"]
    assert v["grid"]["complete"]


def test_a_stored_future_without_the_underlyings_close_gives_no_carry(no_bhav):
    from datetime import date

    from finresearch.fnobook import scenarios

    _paper_spread()
    _future(date(2026, 10, 9), 25010.0, None)
    v = run(scenarios.view(NOW, sources=Sources(chain(strikes=(25000, 25200))), capital=500_000))
    assert v["forwards"][0]["carry"] is None and not v["grid"]["complete"]


def test_each_expiry_is_discounted_at_its_own_tenors_rate_not_the_longest_legs(monkeypatch):
    """#480: signals.rates is asked at each expiry's own tenor; the old rule read one rate at the longest leg's tenor
    and priced every expiry at it."""
    from datetime import date

    from finresearch.db import session_scope
    from finresearch.fnobook import book, scenarios
    from finresearch.signals import rates
    from finresearch.signals.fno import LegIn

    asked = []

    async def fake(t):
        asked.append(round(t * 525_600))  # minutes
        return {"known": True, "rate": 0.05 + 0.02 * t, "par_yield": None, "tenor_years": t, "as_of": "2026-10-09",
                "fallback": False, "source": f"a test curve at {t:.4f} years", "note": "test"}  # fmt: skip

    monkeypatch.setattr(rates, "risk_free", fake)
    far, t_far = date(2027, 3, 30), (169 * 1440 + 270) / 525_600  # 12-Oct 11:00 to 30-Mar-2027 15:30 IST
    r_near, r_far = 0.05 + 0.02 * T, 0.05 + 0.02 * t_far
    _paper_spread()
    with session_scope() as s:
        book.record(s, symbol="NIFTY", expiry=far, lot=75, legs_in=[LegIn("call", 25000, "buy", 1)],
                    quotes=[book.Quote(900.0, "test")], slippage_pct=1.0, brokerage=20, now=NOW)  # fmt: skip
    src = Sources({EXPIRY: chain(r=r_near), far: chain(expiry=far, f=25300.0, t=t_far, r=r_far)})
    v = run(scenarios.view(NOW, sources=src, capital=500_000))
    assert sorted(asked) == [21870, 243630]
    rows = {x["expiry"]: x for x in v["forwards"]}
    assert rows["2026-10-27"]["rate"] == pytest.approx(r_near) and rows["2027-03-30"][
        "rate"
    ] == pytest.approx(r_far)
    # each leg's IV is inverted at its own expiry's rate, so both recover the 14 % their mids were built at
    ivs = {(x["expiry"], x["strike"]): x["iv"] for x in v["legs"]}
    assert ivs[("2026-10-27", 25000)] == pytest.approx(0.14, abs=1e-6)
    assert ivs[("2027-03-30", 25000)] == pytest.approx(0.14, abs=1e-6)
    assert rows["2027-03-30"]["carry"] == pytest.approx(math.log(25300 / SPOT) / t_far, rel=1e-9)
    assert v["grid"]["complete"]
