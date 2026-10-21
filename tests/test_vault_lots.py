"""The dated index lot table (#365, backtest.lots): each expected lot read by hand from the NSE circular named beside it
(NSE/FAOP numbers; nsearchives.nseindia.com/content/circulars/). Unsourced or ambiguous cases are None (unknown), and
a None lot makes the backtest skip the trade (PREREG.md §6: unknown lot = no entry)."""

from __future__ import annotations

from datetime import date

import pytest

from finresearch.backtest import lots

D = date


@pytest.mark.parametrize(
    ("symbol", "expiry", "day", "kind", "lot"),
    [
        (
            "NIFTY",
            D(2008, 6, 26),
            D(2008, 6, 2),
            "monthly",
            50,
        ),  # FAOP/8483: 100 -> 50, all contracts, 23-Feb-2007
        ("BANKNIFTY", D(2008, 1, 31), D(2008, 1, 2), "monthly", 25),  # FAOP/9871: 50 -> 25, all, 28-Dec-2007
        # FAOP/11720: 25 -> 50 for the far month (Mar-2009) only; Jan and Feb 2009 keep 25
        ("BANKNIFTY", D(2009, 2, 26), D(2009, 1, 15), "monthly", 25),
        ("BANKNIFTY", D(2009, 3, 26), D(2009, 1, 15), "monthly", 50),
        ("BANKNIFTY", D(2009, 4, 30), D(2009, 2, 27), "monthly", 50),  # listed after the revision
        # FAOP/14481: 50 -> 25, all contracts from 30-Apr-2010
        ("BANKNIFTY", D(2010, 5, 27), D(2010, 4, 29), "monthly", 50),
        ("BANKNIFTY", D(2010, 5, 27), D(2010, 4, 30), "monthly", 25),
        # FAOP/27733: NIFTY 50 -> 25, all contracts from 31-Oct-2014
        ("NIFTY", D(2014, 11, 27), D(2014, 10, 30), "monthly", 50),
        ("NIFTY", D(2014, 11, 27), D(2014, 10, 31), "monthly", 25),
        # FAOP/30449: NIFTY 25 -> 75, BANKNIFTY 25 -> 30 from the Nov-2015 far month; Sep and Oct keep 25
        ("NIFTY", D(2015, 10, 29), D(2015, 9, 15), "monthly", 25),
        ("NIFTY", D(2015, 11, 26), D(2015, 9, 15), "monthly", 75),
        ("BANKNIFTY", D(2015, 12, 31), D(2015, 10, 15), "monthly", 30),
        # FAOP/32329: BANKNIFTY weeklies 30 for every June-2016 expiry, 40 from July-2016 (FAOP/32134's 30 -> 40)
        ("BANKNIFTY", D(2016, 6, 9), D(2016, 6, 1), "weekly", 30),
        ("BANKNIFTY", D(2016, 7, 7), D(2016, 6, 1), "weekly", 40),
        ("BANKNIFTY", D(2016, 7, 28), D(2016, 5, 2), "monthly", 40),
        # FAOP/44039: BANKNIFTY 20 -> 25 from the Jul-2020 far month; May and Jun 2020 keep 20
        ("BANKNIFTY", D(2020, 6, 25), D(2020, 6, 1), "monthly", 20),
        ("BANKNIFTY", D(2020, 7, 30), D(2020, 6, 1), "monthly", 25),
        ("BANKNIFTY", D(2020, 8, 6), D(2020, 7, 1), "weekly", 25),  # listed after the revision
        # FAOP/47854: NIFTY 75 -> 50 from the Jul-2021 monthly and the Aug-2021 weeklies
        ("NIFTY", D(2021, 6, 24), D(2021, 6, 1), "monthly", 75),
        ("NIFTY", D(2021, 7, 29), D(2021, 6, 1), "monthly", 50),
        ("NIFTY", D(2021, 7, 8), D(2021, 7, 1), "weekly", 75),
        ("NIFTY", D(2021, 8, 5), D(2021, 7, 1), "weekly", 50),
        (
            "FINNIFTY",
            D(2021, 3, 25),
            D(2021, 3, 1),
            "monthly",
            40,
        ),  # FAOP/46603: launched 11-Jan-2021, lot 40
        # long-term NIFTY options (expiry > 3 months): FAOP/28360 kept them at 50 after the 2014 cut until each became
        # the far month (Mar-2015: 26-Dec-2014); FAOP/30450 moved them 50 -> 75 on 28-Sep-2015; FAOP/47854 75 -> 50
        # on 25-Jun-2021
        ("NIFTY", D(2015, 3, 26), D(2014, 12, 24), "monthly", 50),
        ("NIFTY", D(2015, 3, 26), D(2014, 12, 26), "monthly", 25),
        (
            "NIFTY",
            D(2015, 6, 25),
            D(2015, 3, 20),
            "monthly",
            50,
        ),  # by the same rule: far month after 26-Mar-2015
        ("NIFTY", D(2015, 6, 25), D(2015, 3, 27), "monthly", 25),
        (
            "NIFTY",
            D(2012, 12, 27),
            D(2010, 6, 1),
            "monthly",
            50,
        ),  # long-term in 2010: the 2007 lot, all contracts
        ("NIFTY", D(2015, 12, 31), D(2015, 9, 15), "monthly", 50),
        ("NIFTY", D(2015, 12, 31), D(2015, 9, 28), "monthly", 75),
        ("NIFTY", D(2021, 9, 30), D(2021, 6, 1), "monthly", 75),
        ("NIFTY", D(2021, 9, 30), D(2021, 6, 25), "monthly", 50),
        # FAOP/39012: BANKNIFTY 40 -> 20 from the Jan-2019 far month; before 26-Oct-2018 every contract is 40
        ("BANKNIFTY", D(2018, 11, 29), D(2018, 10, 1), "monthly", 40),
        ("BANKNIFTY", D(2019, 1, 31), D(2018, 11, 15), "monthly", 20),
    ],
)
def test_lot_matches_the_circular(symbol, expiry, day, kind, lot):
    assert lots.lot_for(symbol, expiry, day, kind) == lot


@pytest.mark.parametrize(
    ("symbol", "expiry", "day", "kind"),
    [
        ("FINNIFTY", D(2021, 1, 28), D(2020, 12, 31), "monthly"),  # before its launch
        ("NIFTY", D(2022, 1, 27), D(2022, 1, 3), "monthly"),  # after the table (the app's own lots apply)
        ("MIDCPNIFTY", D(2021, 1, 28), D(2021, 1, 4), "monthly"),  # not in the table
        ("NIFTY", D(2007, 1, 25), D(2007, 1, 2), "monthly"),  # before the first sourced row
        # FAOP/44039 is silent on weeklies: a July-2020 BANKNIFTY weekly could be 20 or 25
        ("BANKNIFTY", D(2020, 7, 9), D(2020, 7, 1), "weekly"),
        # FAOP/39012 both keeps Nov/Dec-2018 at 40 and limits that rule to upward revisions (this was a cut)
        ("BANKNIFTY", D(2018, 11, 29), D(2018, 10, 29), "monthly"),
        ("BANKNIFTY", D(2018, 11, 8), D(2018, 10, 29), "weekly"),
    ],
)
def test_unsourced_lots_are_unknown(symbol, expiry, day, kind):
    assert lots.lot_for(symbol, expiry, day, kind) is None


def test_every_row_cites_an_nse_circular_and_weeklies_are_dated():
    for r in lots.REVISIONS:
        assert (
            r.source.startswith("https://")
            and "nseindia.com" in r.source
            and r.circular.startswith("NSE/FAOP/")
        )
    assert lots.WEEKLIES_FROM["BANKNIFTY"].first_expiry == D(2016, 6, 2)  # FAOP/32329
    assert lots.WEEKLIES_FROM["NIFTY"].first_expiry == D(2019, 2, 14)  # FAOP/39894
    assert lots.WEEKLIES_FROM["FINNIFTY"].first_trade == D(2021, 1, 11)  # FAOP/46603


def test_history_reads_vault_lots_from_the_table_never_the_oi_gcd(env, db_url, monkeypatch):
    """DbHistory.lot: on a vault day the circular table decides, even against an inferred lot in fno_contract (the
    gcd of OI can return a multiple); a day the table leaves unknown is None. Later days keep the stored lot."""
    from sqlalchemy import text

    from finresearch.backtest.dbhistory import DbHistory
    from finresearch.db import session_scope
    from finresearch.db.models import FnoContract
    from finresearch.vault import guard

    monkeypatch.setattr(
        guard, "check_grant", lambda g, url: True
    )  # the guard itself is tested in test_vault_guard
    with session_scope(db_url) as s:
        s.execute(text("TRUNCATE fno_contract"))
        for sym, exp, kind, lot in [("NIFTY", D(2015, 11, 26), "monthly", 25), ("BANKNIFTY", D(2018, 11, 29), "monthly", 20),
                                    ("BANKNIFTY", D(2020, 7, 9), "weekly", 25), ("NIFTY", D(2022, 1, 27), "monthly", 50)]:  # fmt: skip
            s.add(FnoContract(symbol=sym, expiry=exp, underlying_kind="index", kind=kind, first_seen=exp,
                              last_seen=exp, lot_size=lot, lot_source="inferred_oi_gcd"))  # fmt: skip
    h = DbHistory(db_url)
    try:
        assert h.lot("NIFTY", D(2015, 11, 26), D(2015, 9, 15)) == 75  # FAOP/30449, not the gcd's 25
        assert h.lot("BANKNIFTY", D(2018, 11, 29), D(2018, 10, 29)) is None  # ambiguous: no entry
        assert h.lot("BANKNIFTY", D(2020, 7, 9), D(2020, 7, 1)) is None  # weekly kind read from fno_contract
        assert h.lot("BANKNIFTY", D(2016, 7, 28), D(2016, 5, 2)) == 40  # no fno_contract row: the table alone
        assert (
            h.lot("NIFTY", D(2022, 1, 27), D(2022, 1, 3)) == 50
        )  # after the vault: the stored lot, as before
    finally:
        with session_scope(db_url) as s:
            s.execute(text("TRUNCATE fno_contract"))
