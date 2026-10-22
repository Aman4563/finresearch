"""#366 acceptance: the 3-month rate from the G-sec par curve within 15 bp of FBIL's 3-month T-bill (both
continuous), and the approximate total-return index read, labelled as approximate. Synthetic rows; expected values
computed here from the formulas."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

D1, D2, D3 = date(2022, 1, 3), date(2022, 1, 4), date(2022, 1, 5)


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE rate_curve, index_eod"))
    yield env


def _rate(s, series, day, tenor, years, r):
    from finresearch.db.models import RateCurve

    s.add(RateCurve(series=series, day=day, tenor=tenor, tenor_years=Decimal(years), rate=Decimal(r)))


def _par(y):
    return 2 * math.log(1 + y / 2)


def _tb(y):
    return math.log(1 + y * 91 / 365) * 365 / 91


def test_par_3m_vs_tbill_3m_within_15bp(clean):
    from finresearch.db import session_scope
    from finresearch.refseries import checks

    with session_scope() as s:
        for d, par, tb in ((D1, "0.0400", "0.0395"), (D2, "0.0400", "0.0350")):
            _rate(s, "gsec_par", d, "0.25Y", "0.25", par)
            _rate(s, "tbill", d, "3M", "0.2493", tb)
        _rate(s, "gsec_par", D3, "0.25Y", "0.25", "0.0400")  # no T-bill that day: not a pair
    with session_scope() as s:
        res = checks.par3m_vs_tbill(s)
    gaps = [(_par(0.04) - _tb(0.0395)) * 1e4, (_par(0.04) - _tb(0.035)) * 1e4]
    assert res["pairs"] == 2 and res["within"] == 1 and res["tolerance_bp"] == 15
    assert res["worst"][0]["day"] == "2022-01-04" and res["worst"][0]["gap_bp"] == pytest.approx(
        gaps[1], abs=0.01
    )
    assert res["median_gap_bp"] == pytest.approx(sum(gaps) / 2, abs=0.01)
    assert "assumed" in res["tbill_basis"]


def test_par3m_vs_tbill_with_nothing_stored_is_unknown(clean):
    from finresearch.db import session_scope
    from finresearch.refseries import checks

    with session_scope() as s:
        res = checks.par3m_vs_tbill(s)
    assert res["pairs"] == 0 and res["within_share"] is None


def test_tri_series_is_the_labelled_approximation(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import IndexEod
    from finresearch.refseries.tri import tri_series

    with session_scope() as s:
        for d, close, y in ((D1, "100", "1.20"), (D2, "101", "1.20"), (D3, "102", None)):
            s.add(IndexEod(day=d, symbol="NIFTY", index_name="Nifty 50", close=Decimal(close),
                           div_yield=None if y is None else Decimal(y)))  # fmt: skip
    with session_scope() as s:
        got = tri_series(s, "NIFTY", D1, D3)
    assert got["official"] is False and "APPROXIMATE" in got["method"]
    tr2 = 100 * (101 / 100 + 0.012 / 252)
    assert got["rows"][0] == ("2022-01-03", pytest.approx(100.0))
    assert got["rows"][1] == ("2022-01-04", pytest.approx(tr2))
    assert got["rows"][2] == ("2022-01-05", None)  # unknown yield: the chain stops, never 0
    assert "2022-01-05" in got["note"]
