"""The registered betas in the book's scenarios and on the card (#394, #395): the scenario view, the book's shock and
one set's shock read each underlying's estimate and show it. Synthetic chains, legs and estimates; no network."""

from __future__ import annotations

import math
from datetime import date

import pytest
from sqlalchemy import text
from test_book_scenarios_api import (  # noqa: F401
    NOW,
    VOLS,
    B,
    F,
    R,
    Sources,
    T,
    _b76,
    _paper_spread,
    chain,
    groww_market,
)
from test_fno_book import EXPIRY, book_db, offline, run  # noqa: F401
from test_risk_budget_book_395 import _bs

from finresearch.fincalc.beta import BetaEstimate

pytestmark = pytest.mark.usefixtures("book_db")
T1 = 14 / 365  # Mon 12-Oct 11:00 -> Tue 13-Oct 15:30, 14 days before 27-Oct 15:30
BAND = BetaEstimate("BANKNIFTY", 1.15, 0.09, 1.0, 1.3, n=240, missing=3, n_eff=88.0,
                    window_start=date(2025, 10, 1), as_of=date(2026, 10, 9), half_life=63)  # fmt: skip


def _banknifty_spread() -> None:
    from finresearch.db import session_scope

    _paper_spread()  # long 75 x 25000 CE, short 75 x 25200 CE
    with session_scope() as s:
        s.execute(text("UPDATE paper_strategy SET symbol = 'BANKNIFTY'"))


@pytest.fixture
def band(monkeypatch):
    from finresearch.fnohist import betas

    seen = []

    def load(underlyings, today):
        seen.append((sorted(underlyings), today))
        return {"BANKNIFTY": BAND}

    monkeypatch.setattr(betas, "load", load)
    return seen


def test_without_a_stored_series_the_view_says_why_the_beta_is_unknown():
    from finresearch.fnobook import scenarios

    _banknifty_spread()
    v = run(scenarios.view(NOW, sources=Sources(chain()), capital=500_000))
    assert not v["grid"]["complete"]
    why = "BANKNIFTY: beta to NIFTY unknown, NIFTY's front-future series is empty"
    assert why in v["grid"]["reasons"]
    (b,) = v["betas"]
    assert b["underlying"] == "BANKNIFTY" and b["beta"] is None and b["reason"] == why


def test_the_view_prices_a_fall_at_the_band_s_upper_end_and_shows_the_estimate(band):
    from finresearch.fnobook import scenarios

    _banknifty_spread()
    v = run(scenarios.view(NOW, sources=Sources(chain()), capital=500_000))
    assert band == [(["BANKNIFTY"], date(2026, 10, 12))]
    assert v["grid"]["complete"], v["grid"]["reasons"]
    cell = next(c for c in v["grid"]["cells"] if (c["move"], c["dvol"], c["horizon"]) == (-0.05, 0.0, "1d"))
    f1, dv = (
        F * (1 - 1.3 * 0.05) * math.exp(-B * (T - T1)),  # #480: rolled at the expiry's carry B
        1.04 * 6.5 / 100,
    )  # β_hi x 5 % fall, +6.76 vol pts
    want = sum(q * (_b76("call", f1, k, T1, VOLS[k] + dv) - _b76("call", F, k, T, VOLS[k]))
               for k, q in ((25000, 75), (25200, -75)))  # fmt: skip
    assert cell["pnl"] == pytest.approx(want, abs=0.02) and cell["ends"] == {"BANKNIFTY": "hi"}
    (b,) = v["betas"]
    assert b == {"underlying": "BANKNIFTY", "beta": 1.15, "se": 0.09, "lo": 1.0, "hi": 1.3, "n": 240, "missing": 3,
                 "window_start": "2025-10-01", "as_of": "2026-10-09", "half_life": 63, "z": 1.645, "floor": 1.0,
                 "reason": None}  # fmt: skip
    assert v["exposures"]["nifty_delta_reasons"] == ["NIFTY's level is unknown"]  # the point β is known


def test_the_book_shock_reports_the_estimates_it_used(band):
    from finresearch.fnobook import scenarios

    _banknifty_spread()
    used: dict = {}
    got, notes = run(scenarios.book_shock(NOW, sources=Sources(chain()), betas_out=used))
    assert got is not None, notes
    assert used == {"BANKNIFTY": BAND}


def test_one_set_of_a_banknifty_card_is_shocked_at_its_band_and_shown_on_the_card():
    from finresearch.fincalc import options as o
    from finresearch.signals import tradeplan as tpl

    s0, k, v = 52000.0, 50000.0, 0.16
    mark = _bs("put", s0, k, T, R, v)
    one, notes, _, _ = tpl.trade_terms("BANKNIFTY", EXPIRY, s0, [o.Leg("put", k, mark, -30)], NOW, R, True,
                                       betas={"BANKNIFTY": BAND})  # fmt: skip
    assert one is not None, notes
    for m, b in (
        (-0.05, 1.3),
        (-0.10, 1.3),
        (0.05, 1.0),
        (-0.03, 1.3),
    ):  # a short put: the larger fall, the smaller rise
        dv = (-1.04 * b * m * 100 if m < 0 else -0.76 * b * m * 100) / 100
        assert one[m] == pytest.approx(
            (_bs("put", s0 * (1 + b * m), k, T1, R, v + dv) - mark) * -30, rel=1e-4
        )
    ob = tpl.book_for(tpl.EMPTY_BOOK, 1_000_000.0, "BANKNIFTY", "put_credit", s0, None,
                      (one, notes, 1.0, ()), betas={"BANKNIFTY": BAND})  # fmt: skip
    from finresearch.fincalc import tradeplan as tp

    n, why = tp.shock_lots(tpl.EMPTY_BOOK.shock, one, 200_000.0)  # known now: no longer "unknown, no room"
    assert n is not None and n >= 1, why
    (shown,) = tpl.book_json(ob)["shock_betas"]
    assert shown["underlying"] == "BANKNIFTY" and shown["hi"] == 1.3 and shown["as_of"] == "2026-10-09"


def test_card_betas_read_the_store_and_never_invent_a_beta():
    from finresearch.signals import tradeplan as tpl

    assert tpl.card_betas("NIFTY", date(2026, 10, 12)) == {}
    got = tpl.card_betas("BANKNIFTY", date(2026, 10, 12))["BANKNIFTY"]
    assert not got.known and "front-future series is empty" in got.reason


@pytest.mark.parametrize(
    ("raised", "needle"),
    [
        (RuntimeError("db down"), "the stored futures could not be read (RuntimeError)"),
        (None, "differs from its registration"),
    ],
)
def test_an_unreadable_store_or_an_unregistered_estimator_leaves_beta_unknown(monkeypatch, raised, needle):
    from finresearch.fincalc import beta as fb
    from finresearch.fnobook import scenarios
    from finresearch.fnohist import betas

    def load(underlyings, today):
        raise raised if raised is not None else fb.PreregistrationError("pin differs")

    monkeypatch.setattr(betas, "load", load)
    _banknifty_spread()
    v = run(scenarios.view(NOW, sources=Sources(chain()), capital=500_000))
    (b,) = v["betas"]
    assert b["beta"] is None and needle in b["reason"] and not v["grid"]["complete"]
