"""The volatility race's loader (P3.7, #390; volrace/data.py) on synthetic bhavcopy rows in the test database: the
front-future bars with the same contract's previous close across a roll, the per-expiry ATM IV term structure from
option settles, and the vault and registration refusals."""

from datetime import date
from decimal import Decimal as D

import pytest

from finresearch.fincalc import fno_history as fh

E1, E2 = date(2025, 1, 30), date(2025, 2, 27)
DAYS = [date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30), date(2025, 1, 31)]


def fut(s, day, expiry, o, h, lo, c):
    from finresearch.db.models import FnoBhav

    s.add(FnoBhav(day=day, symbol="NIFTY", instrument="IDF", expiry=expiry, strike=D(0), option_type="XX",
                  open=D(str(o)), high=D(str(h)), low=D(str(lo)), close=D(str(c)), settle=D(str(c)), contracts=500))  # fmt: skip


def opt(s, day, expiry, strike, kind, price):
    from finresearch.db.models import FnoBhav

    p = D(str(round(price, 2)))
    s.add(FnoBhav(day=day, symbol="NIFTY", instrument="IDO", expiry=expiry, strike=D(strike), option_type=kind,
                  close=p, settle=p, contracts=100))  # fmt: skip


def seed(s) -> None:
    near = [(100, 101, 99, 100.5), (100.4, 102, 100, 101.0), (101.2, 101.5, 99.5, 100.0), None]
    nxt = [(101, 102, 100, 101.5), None, (102.0, 102.6, 100.6, 101.1), (101.5, 103, 101, 102.5)]
    for d, a, b in zip(DAYS, near, nxt, strict=True):
        if a:
            fut(s, d, E1, *a)
        if b:
            fut(s, d, E2, *b)
    d = DAYS[0]  # options on the first day: both expiries ATM at 100 with IV 12 % and 15 %
    for e, f, iv in ((E1, 100.5, 0.12), (E2, 101.5, 0.15)):
        t = (e - d).days / 365
        for k in (100,):
            for kind, right in (("CE", "call"), ("PE", "put")):
                opt(s, d, e, k, kind, fh.black76_price(right, f, k, t, fh.RATE, iv))


def test_loader_builds_within_contract_bars_across_the_roll_and_the_iv_term(env) -> None:
    from finresearch.db import session_scope
    from finresearch.volrace import data

    with session_scope() as s:
        seed(s)
    with session_scope() as s:
        ss, counts = data.load_sessions(s, "NIFTY", date(2025, 1, 1), date(2025, 2, 28))
    assert [x.day for x in ss] == DAYS
    b = [x.bar for x in ss]
    assert b[0].prev_close is None  # no earlier session of the contract
    assert (b[1].open, b[1].prev_close) == (100.4, 100.5)  # the near contract
    assert (
        b[2].prev_close == 101.0
    )  # expiry day: still the near contract (nearest expiry on or after the day)
    # after the roll the front is February, paired with its own close on 30-Jan (never January's)
    assert b[3].open == 101.5 and b[3].prev_close == 101.1
    assert counts["no_prev_close"] == 1
    term = dict(ss[0].term)
    assert term[(E1 - DAYS[0]).days] == pytest.approx(12.0, abs=0.05)
    assert term[(E2 - DAYS[0]).days] == pytest.approx(15.0, abs=0.05)
    assert ss[1].term == () and counts["no_iv"] == 3


def test_loader_refuses_the_vault_and_an_unregistered_spec(monkeypatch) -> None:
    from finresearch.vault.registry import VaultSealed
    from finresearch.volrace import data, prereg, race

    with pytest.raises(VaultSealed):
        data.load_sessions(None, "NIFTY", date(2021, 12, 31), date(2022, 6, 30))  # type: ignore[arg-type]
    monkeypatch.setattr(race, "TRAIL", 21)
    with pytest.raises(prereg.PreregistrationError):
        data.load_sessions(None, "NIFTY", date(2022, 1, 1), date(2022, 6, 30))  # type: ignore[arg-type]
