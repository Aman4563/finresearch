"""The option screens over stored bhavcopy rows (#368, dq.options): the liquidity filter (fno_history.market_settle)
keeps thinly traded strikes and NSE's theoretical settles out, the same-expiry future gives the forward when parity
cannot, and the screens ride on fno_bhav's checks without ever making the dataset critically unknown. Synthetic rows
(dq_synth, made-up symbols); test database only."""

from __future__ import annotations

import math
from decimal import Decimal

import dq_synth as ds
import pytest
from sqlalchemy import text

TABLES = "fno_bhav, fno_bhav_day, dataset_load"
NAMES = ("opt_bounds", "opt_strike_monotone", "opt_butterfly", "opt_parity", "opt_calendar")


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))
    yield env
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))


def store(rows: list[dict]) -> None:
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))
        ds.manifest(s, ds.T1)
        ds.insert_bhav(s, ds.bhav_rows(ds.T0))
        ds.insert_bhav(s, rows)


def screened() -> dict:
    from finresearch.db import session_scope
    from finresearch.dq import options

    with session_scope() as s:
        return {c.check: c for c in options.option_checks(s, ds.T1)}


def nifty(rows, expiry, strike, right) -> dict:
    return next(r for r in rows if r["instrument"] == "IDO" and r["expiry"] == expiry and r["strike"] == strike
                and r["option_type"] == right)  # fmt: skip


def test_stored_clean_day_passes_every_screen(clean):
    store(ds.bhav_rows(ds.T1))
    res = screened()
    assert {k: (c.status, c.value) for k, c in res.items()} == dict.fromkeys(NAMES, ("ok", 0))
    # 12 options; E1 (F 25,075, ATM 25,000) has one call pair and one put pair, E2 (F 25,175.4, ATM 25,200) two put
    # pairs and one put triple; parity on 3 strikes per expiry plus 2 futures; one calendar pair (27 and 55 days)
    lead = {k: res[k].note.split(" ", 3)[:3] for k in NAMES}
    assert lead == {"opt_bounds": ["0", "of", "12"], "opt_strike_monotone": ["0", "of", "4"],
                    "opt_butterfly": ["0", "of", "1"], "opt_parity": ["0", "of", "8"],
                    "opt_calendar": ["0", "of", "1"]}  # fmt: skip


@pytest.mark.parametrize("contracts,gap,flagged", [(10, 0, True), (9, 0, False), (500, 25, False)])
def test_thin_and_theoretical_settles_stay_out(clean, contracts, gap, flagged):
    """A 25,400 call on E1 priced 500 over the 25,200 call: a gross break, flagged only when its settle is a market
    price (10+ contracts and the settle within 0.5 % + 0.05 of the close); 9 contracts, or a settle 25 off the close
    (NSE's model value), keep it out of every screen."""
    rows = ds.bhav_rows(ds.T1)
    c = nifty(rows, ds.E1, 25200, "CE")
    px = c["close"] + 500
    bad = {
        **c,
        "strike": Decimal(25400),
        "close": px,
        "settle": px + Decimal("0.05") + gap,
        "contracts": contracts,
    }
    store([*rows, bad])
    mono = screened()["opt_strike_monotone"]
    assert (mono.status, mono.value) == (("warn", 1) if flagged else ("ok", 0))
    assert mono.note.startswith("1 of 5 " if flagged else "0 of 4 ")


def stock_chain(with_future: bool) -> list[dict]:
    """ABCSTK E1 options on two strikes (too few for a parity fit): the 1,100 call is priced 50, about 53 under its
    intrinsic value off the future (1,203.65); the other three at Black-76 off that future."""
    rows = [
        r
        for r in ds.bhav_rows(ds.T1)
        if with_future or not (r["symbol"] == "ABCSTK" and r["expiry"] == ds.E1)
    ]
    f = 1200 * 1.003 + 0.05
    t = (ds.E1 - ds.T1).days / 365
    for k in (1100, 1300):
        for right in ("CE", "PE"):
            px = (
                50.0
                if (k, right) == (1100, "CE")
                else ds.option_price(ds.T1, ds.E1, k, {"CE": "call", "PE": "put"}[right], f)
            )
            r = ds._row(ds.T1, "ABCSTK", "STO", ds.E1, k, right, px, 500, 20, 10)
            rows.append(r)
    # the weakest lower bound 103.65 x e^(-0.2 x 27/365) = 102.13: 52.1 short against 0.10 + 0.005 x 1,203.65 = 6.12
    assert math.exp(-0.2 * t) * (f - 1100) - 50 > 0.10 + 0.005 * f
    return rows


def test_a_stock_chain_too_thin_for_parity_is_screened_against_its_future(clean):
    store(stock_chain(with_future=True))
    b = screened()["opt_bounds"]
    assert (b.status, b.value) == (
        "warn",
        1,
    ) and "ABCSTK 27-Oct-2026 1100 CE 50.05 under 102.13" in b.note  # settle = close + 0.05


def test_an_expiry_without_any_forward_is_counted_not_passed(clean):
    store(stock_chain(with_future=False))
    b = screened()["opt_bounds"]
    assert (b.status, b.value) == ("ok", 0) and b.note.startswith("0 of 12 ")
    assert b.note.endswith("1 expiry(ies) without a forward not screened")


def test_screens_ride_on_the_bhavcopy_checks_as_non_critical(clean):
    from finresearch.db import session_scope
    from finresearch.dq import fno

    store(ds.bhav_rows(ds.T1))
    with session_scope() as s:
        res = {c.check: c for c in fno.bhav_checks(s, ds.T1)}
        missing = {c.check: c for c in fno.bhav_checks(s, ds.T1.replace(day=1))}  # a day never loaded
    assert {k: (res[k].status, res[k].critical) for k in NAMES} == dict.fromkeys(NAMES, ("ok", False))
    assert {k: (missing[k].status, missing[k].critical) for k in NAMES} == dict.fromkeys(
        NAMES, ("unknown", False)
    )


def boom_sql(s, day):
    s.execute(text("SELECT 1 / 0"))  # without its own savepoint this would abort the whole data-quality pass


def test_a_crashing_screen_leaves_the_bhavcopy_checks_intact(clean, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.dq import fno, options

    store(ds.bhav_rows(ds.T1))
    monkeypatch.setattr(options, "option_checks", boom_sql)
    with session_scope() as s:
        res = {c.check: c for c in fno.bhav_checks(s, ds.T1)}
        assert s.execute(text("SELECT 1")).scalar() == 1  # the session is still usable
    for k in NAMES:  # the exception's type only: a message could carry row values
        assert (res[k].status, res[k].critical, res[k].note) == ("unknown", False, "screen failed: DataError")
    assert (res["futures_continuity"].status, res["futures_continuity"].critical) == ("ok", True)
