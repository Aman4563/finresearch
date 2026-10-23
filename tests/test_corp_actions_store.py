"""The stored per-stock corporate-action factor history (#369 slice 2, refseries.corp_actions): loading NSE rows with
their factors and lineage, rights priced from the stored cum close, the F&O factor lookup with symbol changes, and the
polite resumable backfill. Test database only; NSE mocked with respx; synthetic symbols (EXDEMO, OLDCO/NEWCO) and
made-up ISINs."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest
from sqlalchemy import select, text

EVENING = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)  # 19:30 IST, a Wednesday
URL = "https://www.nseindia.com/api/corporates-corporateActions?index=equities&symbol=EXDEMO"


def row(subject: str, ex: str, symbol: str = "EXDEMO", fv: str = "10") -> dict:
    return {"symbol": symbol, "subject": subject, "exDate": ex, "recDate": ex, "faceVal": fv,
            "isin": "INE000X01019", "series": "EQ", "comp": "Example Demo Limited"}  # fmt: skip


ROWS = [
    row("Bonus 1:1", "02-Jun-2015"),
    row("Bonus 1:1", "02-Jun-2015"),  # NSE repeats rows
    row("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", "10-Mar-2017"),
    row("Demerger", "05-Jan-2019", fv="2"),
    row("Dividend - Rs 5 Per Share", "05-Jan-2019", fv="2"),
    row("Interim Dividend - Rs 3 Per Share", "20-Oct-2020", fv="2"),
]


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    tables = "corp_action, corp_action_fetch, dataset_load, equity_eod"
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {tables}"))
    yield env
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {tables}"))


def load(rows=ROWS, symbol="EXDEMO") -> int:
    from finresearch.db import session_scope
    from finresearch.refseries import corp_actions as ca

    with session_scope() as s:
        return ca.load_nse(s, symbol, rows, url=URL, body=json.dumps(rows).encode())


def test_load_stores_factors_dedupes_and_records_lineage(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import CorpAction, CorpActionFetch, DatasetLoad

    assert load() == 5
    assert load() == 5  # idempotent
    with session_scope() as s:
        got = {
            (r.ex_date, r.kind): (r.status, r.price_factor, r.reason) for r in s.scalars(select(CorpAction))
        }
        assert got[(date(2015, 6, 2), "bonus")] == ("known", D(2), None)
        assert got[(date(2017, 3, 10), "split")] == ("known", D(5), None)
        status, f, reason = got[(date(2019, 1, 5), "demerger")]
        assert status == "unknown" and f is None and "demerged" in reason
        assert got[(date(2019, 1, 5), "dividend")] == ("none", D(1), None)
        m = s.get(CorpActionFetch, ("nse", "EXDEMO"))
        assert (m.status, m.rows, m.earliest_ex, m.attempts) == ("loaded", 5, date(2015, 6, 2), 2)
        loads = s.scalars(select(DatasetLoad).where(DatasetLoad.dataset == "nse_ca:EXDEMO")).all()
        assert len(loads) == 2 and loads[0].url == URL and loads[0].rows == 5


def test_rights_waits_for_the_cum_close_then_rebuild_prices_it(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import CorpAction, EquityEod
    from finresearch.refseries import corp_actions as ca

    load([row("Rights 1:5 @ Premium Rs 100/-", "15-Mar-2024")])
    with session_scope() as s:
        r = s.scalars(select(CorpAction)).one()
        assert (r.status, r.price_factor) == ("unknown", None) and "cum-rights close" in r.reason
        # the last cum day is Thursday 14-Mar-2024: close 300 (TERP 268.333..., f = 1800/1610)
        s.add(EquityEod(day=date(2024, 3, 14), symbol="EXDEMO", series="EQ", close=D(300)))
        s.add(EquityEod(day=date(2024, 3, 15), symbol="EXDEMO", series="EQ", close=D(270)))
    with session_scope() as s:
        assert ca.rebuild(s) == 1
    with session_scope() as s:
        r = s.scalars(select(CorpAction)).one()
        assert r.status == "known" and r.price_factor == pytest.approx(D(1800) / D(1610))
        assert r.terms["p_cum"] == "300"


def test_fno_factors_compound_per_ex_date_and_keep_unknown_as_none(clean):
    from finresearch.db import session_scope
    from finresearch.refseries import corp_actions as ca

    load([*ROWS, row("Bonus 1:2", "10-Mar-2017"), row("Voluntary Delisting", "01-Jan-2021")])
    with session_scope() as s:
        got = ca.fno_factors(s, ["EXDEMO", "NOSUCH"], date(2015, 1, 1), date(2021, 12, 31))
    # split x5 and bonus 1:2 (x1.5) on one ex-date compound to 7.5; the demerger day is unknown (None, never 1);
    # dividend-only days and the delisting (an exit, not a factor) are not factor days
    assert got == {"EXDEMO": {date(2015, 6, 2): 2.0, date(2017, 3, 10): 7.5, date(2019, 1, 5): None}}


def test_symbol_change_carries_factors_to_the_old_symbol(clean):
    from finresearch.db import session_scope
    from finresearch.refseries import corp_actions as ca

    load([row("Bonus 1:1", "02-Jun-2015", symbol="NEWCO")], symbol="NEWCO")
    csv = "SM_NAME,SM_KEY_SYMBOL,SM_NEW_SYMBOL,SM_APPLICABLE_FROM\nNew Co Limited,OLDCO,NEWCO,15-JAN-2018\n"
    with session_scope() as s:
        assert ca.load_symbol_changes(s, csv, url="https://example.invalid/symbolchange.csv") == 1
        got = ca.fno_factors(s, ["OLDCO"], date(2015, 1, 1), date(2015, 12, 31))
    assert got == {"OLDCO": {date(2015, 6, 2): 2.0}}


def test_a_symbol_change_file_without_the_expected_columns_is_refused(clean):
    from finresearch.adapters.nse import NseError
    from finresearch.db import session_scope
    from finresearch.refseries import corp_actions as ca

    with session_scope() as s, pytest.raises(NseError):
        ca.load_symbol_changes(s, "<html>blocked</html>", url=None)


def test_factor_history_back_adjusts_with_the_known_factors_only(clean):
    from finresearch.db import session_scope
    from finresearch.fincalc.corporate_actions import adjust_prices
    from finresearch.refseries import corp_actions as ca

    load()
    with session_scope() as s:
        h = ca.factor_history(s, "EXDEMO")
    assert h.events == [(date(2015, 6, 2), D(2)), (date(2017, 3, 10), D(5))]
    assert [(d, k) for d, k, _ in h.unknown] == [(date(2019, 1, 5), "demerger")]
    # 1000 before both actions is 1000 / (2 x 5) = 100 in today's units; 150 after both stays 150
    days = [date(2015, 1, 1), date(2018, 1, 1)]
    assert adjust_prices(days, [D(1000), D(150)], h.events) == [D(100), D(150)]
