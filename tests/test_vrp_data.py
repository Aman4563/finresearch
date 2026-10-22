"""The VRP study's loader (P3.6, #389; vrp/data.py) on synthetic bhavcopy rows in the test database: a flat 15 %
volatility chain on two monthly expiries gives sigma^2_MF near 0.0225, the straddle and 25-delta put trades run from
one monthly expiry to the next with marks and the final settlement, India VIX is attached, and the vault and an
unregistered spec are refused."""

import math
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from finresearch.fincalc import fno_history as fh

E1, E2, E3 = date(2025, 1, 30), date(2025, 2, 27), date(2025, 3, 27)
DAYS = [
    date(2025, 1, 27) + timedelta(i) for i in range(40) if (date(2025, 1, 27) + timedelta(i)).weekday() < 5
]


def fwd(i: int) -> float:
    return 22_000.0 * (1 + 0.001 * i)


def seed(s) -> None:
    """Idempotent: the env fixture keeps rows across this module's tests."""
    from sqlalchemy import func, select

    from finresearch.db.models import FnoBhav, IndexEod

    if s.scalar(select(func.count()).select_from(FnoBhav).where(FnoBhav.day == DAYS[0])):
        return
    rows = []
    for i, d in enumerate(DAYS):
        f = fwd(i)
        for e in (E1, E2, E3):
            if e < d:
                continue
            rows.append(FnoBhav(day=d, symbol="NIFTY", instrument="IDF", expiry=e, strike=D(0), option_type="XX",
                                open=D(str(round(f * 0.999, 2))), high=D(str(round(f * 1.004, 2))),
                                low=D(str(round(f * 0.995, 2))), close=D(str(round(f, 2))),
                                settle=D(str(round(f, 2))), contracts=5000))  # fmt: skip
            t = (e - d).days / 365
            for k in range(16_000, 28_001, 200):
                for kind, right in (("CE", "call"), ("PE", "put")):
                    p = round(fh.black76_price(right, f, k, t, fh.RATE, 0.15), 2) if t > 0 else (
                        max(f - k, 0.0) if right == "call" else max(k - f, 0.0))  # fmt: skip
                    if p < 0.05:
                        continue
                    rows.append(FnoBhav(day=d, symbol="NIFTY", instrument="IDO", expiry=e, strike=D(k),
                                        option_type=kind, close=D(str(round(p, 2))), settle=D(str(round(p, 2))),
                                        contracts=100))  # fmt: skip
        rows.append(IndexEod(day=d, symbol="INDIA VIX", index_name="India VIX", close=D("15.5")))
    s.add_all(rows)


def test_loader_builds_days_windows_and_trades(env) -> None:
    from finresearch.db import session_scope
    from finresearch.vrp import data

    with session_scope() as s:
        seed(s)
    x, counts = data.load("NIFTY", date(2025, 1, 1), date(2025, 3, 31), lots=lambda *_: 75, band=None)
    assert [d.day for d in x.days] == DAYS and x.starts == {E1, E2}  # E3 is after the data
    d0 = next(d for d in x.days if d.day == E1)
    assert d0.mf30 == pytest.approx(0.15**2, rel=0.03) and d0.vix == 15.5
    assert d0.mf30_flat == pytest.approx(0.15**2, rel=0.03)
    (st,) = [t for t in x.straddles if t.entry == E1]
    i0 = DAYS.index(E1)
    assert st.expiry == E2 and st.lot == 75 and st.rights == ("call", "put")
    assert st.strike == 22_000 + 200 * round((fwd(i0) - 22_000) / 200)
    assert [m.day for m in st.marks] == [d for d in DAYS if E1 <= d < E2]
    assert st.final == pytest.approx(
        fwd(DAYS.index(E2)), abs=0.01
    )  # the expiring future (settles not uniform)
    (pt,) = [t for t in x.puts if t.entry == E1]
    assert pt.rights == ("put",) and pt.strike < st.strike
    assert x.z_mf[E1] is not None and x.z_atm[E1] is not None
    expect = math.log(st.final / fwd(i0)) / math.sqrt(0.15**2 * 28 / 365)
    assert x.z_atm[E1] == pytest.approx(expect, rel=0.05)
    assert counts.get("invalid_bar", 0) == 0


def test_loader_refuses_the_vault_and_an_unregistered_spec(monkeypatch) -> None:
    from finresearch.vault.registry import VaultSealed
    from finresearch.vrp import collect, data, prereg

    with pytest.raises(VaultSealed):
        data.load("NIFTY", date(2021, 12, 1), date(2022, 6, 30), lots=lambda *_: 50, band=None)
    monkeypatch.setattr(collect, "BAND", 0.75)
    with pytest.raises(prereg.PreregistrationError):
        data.load("NIFTY", date(2022, 1, 1), date(2022, 6, 30), lots=lambda *_: 50, band=None)


def test_loaded_inputs_run_through_the_report(env) -> None:
    from finresearch.db import session_scope
    from finresearch.vrp import data, run

    with session_scope() as s:
        seed(s)
    x, _ = data.load("NIFTY", date(2025, 1, 1), date(2025, 3, 31), lots=lambda *_: 75, band=None)
    out = run.report(x, draws=50, seed=1)
    p = out["periods"]["seen"]
    assert p["primary"]["n"] == 1  # E1's window completes; at E2 only one monthly (E3) is listed: unknown
    assert p["primary"]["unknown_reasons"] == {"fewer than two monthly expiries after the day": 1}
    s = p["collect"]["dated"]["straddle"]
    assert s["none"]["n"] == 1 and s["band"]["n"] == 1 and s["fractional"]["n"] == 1
    assert s["none"]["unknown_reasons"] == {"no final settlement price": 1}  # the E2 -> E3 trade
    # from 24-Feb (3 sessions to E2) the terms roll to E3 and a far month that is not listed: unknown
    assert out["gate"]["2022-2026"]["days"] == len([d for d in DAYS if d < date(2025, 2, 24)])
