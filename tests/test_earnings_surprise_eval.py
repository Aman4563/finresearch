"""The earnings-surprise harvest's filing selection and the experiment's event builder on synthetic data (#181)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from finresearch.evals import earnings_harvest as H
from finresearch.evals import earnings_surprise as E
from finresearch.fincalc import surprise as S


def test_first_reported_keeps_the_original_filing_and_earliest_broadcast():
    results = [  # NSE Financial Results index rows (Example Ltd): standalone first, then consolidated
        {"toDate": "31-Dec-2023", "consolidated": "Non-Consolidated", "broadCastDate": "18-Jan-2024 14:05:10",
         "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/S1.xml"},
        {"toDate": "31-Dec-2023", "consolidated": "Consolidated", "broadCastDate": "18-Jan-2024 16:40:00",
         "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/C1.xml"},
        {"toDate": "30-Sep-2023", "consolidated": "Consolidated", "broadCastDate": "-", "xbrl": "-"},
    ]  # fmt: skip
    integrated = [  # an original and a later revision of the Mar-2025 quarter
        {"type": "Integrated Filing- Financials", "qe_Date": "31-MAR-2025", "consolidated": "Consolidated",
         "broadcast_Date": "25-Apr-2025 19:10:00", "type_Sub": "Original",
         "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/IF1.xml"},
        {"type": "Integrated Filing- Financials", "qe_Date": "31-MAR-2025", "consolidated": "Consolidated",
         "broadcast_Date": "02-Jun-2025 11:00:00", "type_Sub": "Revised",
         "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/IF2.xml"},
    ]  # fmt: skip
    rows = H.filing_rows(results, integrated)
    assert len(rows) == 4  # the row without a broadcast time is dropped
    assert H.choose_basis(rows) is True
    picked = H.first_reported(rows, True)
    dec = picked[date(2023, 12, 31)]
    assert dec["xbrl"].endswith("C1.xml")
    assert (
        dec["announced"].hour == 14
    )  # the standalone came out first: that is when the market learnt the news
    mar = picked[date(2025, 3, 31)]
    assert mar["xbrl"].endswith("IF1.xml") and not mar["revised_only"]
    assert mar["announced"].date() == date(2025, 4, 25)


def _sessions(start: date, end: date) -> list[date]:
    return [
        start + timedelta(days=k)
        for k in range((end - start).days + 1)
        if (start + timedelta(days=k)).weekday() < 5
    ]


SESS = _sessions(date(2021, 6, 1), date(2024, 12, 31))
EPS = [5.0 + (k % 4) + 0.1 * k + (0.4 if k % 3 == 0 else 0) for k in range(25)]  # Sep-2018 .. Sep-2024


def _stock(jump_after: date | None, eps=EPS) -> dict:
    ends = [S.quarter_back(date(2024, 9, 30), 24 - k) for k in range(25)]
    qs = [{"quarter_end": e.isoformat(), "start": None,
           "announced": f"{(e + timedelta(days=20)).isoformat()}T19:00:00+05:30", "has_time": True,
           "filed": f"{(e + timedelta(days=20)).isoformat()}T19:00:00+05:30", "eps": v, "revenue": 100 * v,
           "revised_only": False} for e, v in zip(ends, eps, strict=True)]  # fmt: skip
    prices = [[d.isoformat(), 110.0 if jump_after and d > jump_after else 100.0] for d in SESS]
    return {"quarters": qs, "actions": [], "prices": prices}


def test_build_events_after_close_and_drift():
    # Dec-2022 quarter broadcast Fri 20-Jan-2023 19:00 -> t0 Mon 23-Jan; the stock re-rates after t0+1 (Tue 24-Jan)
    data = {"AAA": _stock(date(2023, 1, 24)), H.MARKET: {"prices": [[d.isoformat(), 50.0] for d in SESS]}}
    events, _ = E.build_events(data, lambda t: frozenset({"AAA"}))
    ev = {e["quarter_end"]: e for e in events}["2022-12-31"]
    assert ev["t0"] == "2023-01-23"
    assert ev["reaction"] == pytest.approx(0.0)  # 20-Jan close -> 24-Jan close: no move yet
    assert ev["drift"] == pytest.approx(0.10)  # 24-Jan close -> t0+60 close: +10 % vs a flat market
    # the SUE is the pure function's, from filings known by then
    eps_q, _ = E.quarters(data["AAA"], "eps")
    assert ev["sue"] == pytest.approx(S.sue(eps_q, date(2022, 12, 31))[0])
    # the latest quarters' +60 windows run past the calendar: no drift, never a made-up one
    assert ev and all(e["drift"] is None for e in events if e["quarter_end"] >= "2024-09-30")


def test_build_events_ignores_future_filings():
    """No look-ahead: changing every figure filed after a quarter's announcement leaves its event unchanged."""
    base = {"AAA": _stock(None), H.MARKET: {"prices": [[d.isoformat(), 50.0] for d in SESS]}}
    later = [*EPS[:16], *[v * 10 for v in EPS[16:]]]  # quarters from Sep-2022 (index 16) on are rewritten
    alt = {"AAA": _stock(None, later), H.MARKET: base[H.MARKET]}
    a = {e["quarter_end"]: e["sue"] for e in E.build_events(base, lambda t: frozenset({"AAA"}))[0]}
    b = {e["quarter_end"]: e["sue"] for e in E.build_events(alt, lambda t: frozenset({"AAA"}))[0]}
    assert a["2022-06-30"] == b["2022-06-30"]
    assert a["2022-09-30"] != b["2022-09-30"]


def test_membership_and_deadline_drops():
    data = {"AAA": _stock(None), H.MARKET: {"prices": [[d.isoformat(), 50.0] for d in SESS]}}
    data["AAA"]["quarters"][14]["announced"] = (
        "2022-09-04T18:30:00+05:30"  # Mar-2022 quarter: a re-upload date
    )
    events, drops = E.build_events(
        data, lambda t: frozenset() if t < date(2022, 1, 1) else frozenset({"AAA"})
    )
    assert drops["broadcast after the legal deadline (re-upload)"] == 1
    assert drops["not a Nifty 50 member at t0"] == 1  # the Sep-2021 quarter, announced Oct-2021
    assert "2022-03-31" not in {e["quarter_end"] for e in events}
