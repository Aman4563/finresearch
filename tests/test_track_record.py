"""#353 (P0.3) track-record integrity: the headline holds no benchmark (market-implied, base-rate) or shadow rows and
is shown per asset; nested direction events of one path count once; an IPO is scored on its last forecast before
17:00 IST on the last bidding day, and that forecast survives a later same-day view; the IPO EV is after listing-day
selling costs. Expected values are worked out by hand here."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from finresearch.fincalc.dates import IST


@pytest.fixture
def ledger_db(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast, IpoIssue

    with session_scope() as s:
        s.query(Forecast).delete()
        s.query(IpoIssue).delete()
    yield tmp_path.name[-6:].upper().replace("_", "")


def at(y, m, d, h=12, mi=0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=IST)


def _sig(asset, sym, p, when, method, status, event="e"):
    from finresearch.signals import Signal, Validation

    return Signal(asset=asset, instrument=sym, name=None, action="APPLY" if asset == "ipo" else "INFORMATIONAL",
                  score=0, event=event, horizon="h", method=method, validation=Validation(status, 30), probability=p,
                  as_of=when)  # fmt: skip


def _log(s, sig, kind, resolve_on, outcome, inputs=None):
    from finresearch.signals import ledger

    fid = ledger.record(sig, source="test", resolve_on=resolve_on, event_kind=kind, session=s, inputs=inputs)
    ledger.resolve(
        s, fid, outcome, now=datetime.combine(resolve_on, datetime.min.time(), IST).replace(hour=17)
    )
    return fid


def _close(s, sym, close_day):
    from finresearch.db.models import IpoIssue

    s.add(IpoIssue(exchange="NSE", issue_key=sym, symbol=sym, name=sym, board="mainboard", open_date=None,
                   close_date=close_day, status="closed", first_seen=at(2026, 8, 1), last_seen=at(2026, 9, 3)))  # fmt: skip
    s.flush()


# ------------------------------------------------------------------------------------------------ the headline
def test_headline_excludes_market_implied_and_base_rate_rows_and_is_per_asset(ledger_db):
    from finresearch.db import session_scope
    from finresearch.direction.live import METHOD_IMPLIED
    from finresearch.signals import ledger
    from finresearch.signals.ipo import BASE_RATE_METHOD

    t = ledger_db
    with session_scope() as s:
        # benchmarks: the option market's own probability (logged as rule_based) and the IPO base-rate table
        _log(
            s,
            _sig("fno", "NIFTY" + t, 0.9, at(2026, 9, 1, 16), METHOD_IMPLIED, "rule_based"),
            "fno_dir_up_h1",
            date(2026, 9, 2),
            0,
            {"symbol": "NIFTY", "start_date": "2026-09-01", "h": 1, "event": "up"},
        )
        _close(s, "IB" + t, date(2026, 9, 3))
        _log(
            s,
            _sig("ipo", "IB" + t, 0.8, at(2026, 9, 3, 11), BASE_RATE_METHOD, "base_rate"),
            "listing_gain",
            date(2026, 9, 8),
            0,
        )
        # calls: a research-run verdict on an IPO and a stock signal
        _close(s, "IC" + t, date(2026, 9, 3))
        _log(
            s,
            _sig("ipo", "IC" + t, 0.75, at(2026, 9, 3, 11), ledger.RUN_METHOD, "uncalibrated"),
            "listing_gain",
            date(2026, 9, 8),
            1,
        )
        _log(
            s,
            _sig("stock", "SC" + t, 0.6, at(2025, 9, 1), "composite", "rule_based"),
            "excess_return_12m",
            date(2026, 9, 1),
            1,
            {"start_date": "2025-09-01"},
        )
        groups = {(g["asset"], g["method"]): g for g in ledger.calibration_groups(s)}
    head = {a: groups[(a, ledger.HEADLINE)] for a in ("ipo", "stock") if (a, ledger.HEADLINE) in groups}
    # per asset: the IPO headline is the run verdict alone, (0.75 − 1)² = 0.0625; no F&O headline (benchmarks only)
    assert head["ipo"]["n"] == 1 and head["ipo"]["brier"] == pytest.approx(0.0625)
    assert head["stock"]["n"] == 1 and head["stock"]["brier"] == pytest.approx(0.16)
    assert ("fno", ledger.HEADLINE) not in groups
    # the cross-asset pool also holds calls only: (0.0625 + 0.16) / 2
    pool = groups[("all", "all methods")]
    assert pool["n"] == 2 and pool["brier"] == pytest.approx(0.11125)
    # the benchmarks keep their own rows
    assert groups[("fno", METHOD_IMPLIED)]["n"] == 1 and groups[("ipo", BASE_RATE_METHOD)]["n"] == 1


def test_nested_direction_events_of_one_path_count_once(ledger_db):
    """P(up), P(up_k) and P(down_k) at h = 5 from one close are three rows about one 5-session path: one cluster.
    The same start at h = 1 is its own cluster (the cluster key is underlying, horizon, start-date window)."""
    from finresearch.db import session_scope
    from finresearch.direction.live import METHOD_IMPLIED
    from finresearch.signals import ledger

    sym = "NIFTY"
    base = {"symbol": sym, "start_date": "2026-09-01"}
    with session_scope() as s:
        for ev, p, o in (("up", 0.55, 1), ("up_k", 0.30, 1), ("down_k", 0.25, 0)):
            _log(
                s,
                _sig("fno", sym, p, at(2026, 9, 1, 16), METHOD_IMPLIED, "rule_based"),
                f"fno_dir_{ev}_h5",
                date(2026, 9, 8),
                o,
                {**base, "h": 5, "event": ev},
            )
        _log(
            s,
            _sig("fno", sym, 0.52, at(2026, 9, 1, 16), METHOD_IMPLIED, "rule_based"),
            "fno_dir_up_h1",
            date(2026, 9, 2),
            0,
            {**base, "h": 1, "event": "up"},
        )
        g = next(g for g in ledger.calibration_groups(s, "fno") if g["method"] == METHOD_IMPLIED)
    assert g["scored_forecasts"] == 4 and g["n"] == 2
    # the cluster keeps P(up) (event priority up, up_k, down_k): ((0.55 − 1)² + (0.52 − 0)²) / 2
    assert g["brier"] == pytest.approx((0.45**2 + 0.52**2) / 2)


# ------------------------------------------------------------------------------------------------ IPO decision time
def test_a_view_after_the_17h_cutoff_keeps_the_earlier_same_day_forecast(ledger_db):
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.signals import ledger
    from finresearch.signals.ipo import BLEND_METHOD

    sym = "CU" + ledger_db
    kw = dict(source="signal:ipo", resolve_on=date(2026, 9, 8), event_kind="listing_gain")
    with session_scope() as s:
        a = ledger.record(
            _sig("ipo", sym, 0.6, at(2026, 9, 3, 16, 59), BLEND_METHOD, "shadow"), session=s, **kw
        )
        b = ledger.record(_sig("ipo", sym, 0.9, at(2026, 9, 3, 18), BLEND_METHOD, "shadow"), session=s, **kw)
        assert a != b and s.get(Forecast, a).probability == 0.6  # the 16:59 forecast is not overwritten
        c = ledger.record(
            _sig("ipo", sym, 0.65, at(2026, 9, 3, 16, 30), BLEND_METHOD, "shadow"), session=s, **kw
        )
        assert c == a and s.get(Forecast, a).probability == 0.6  # an older one never replaces a newer one


def test_an_ipo_is_scored_on_its_last_forecast_before_17h_on_the_last_bidding_day(ledger_db):
    from finresearch.db import session_scope
    from finresearch.signals import ledger
    from finresearch.signals.ipo import BLEND_METHOD

    sym, unknown = "CO" + ledger_db, "UK" + ledger_db
    with session_scope() as s:
        _close(s, sym, date(2026, 9, 3))
        for when, p in ((at(2026, 9, 2, 15), 0.6), (at(2026, 9, 3, 16, 59), 0.7), (at(2026, 9, 3, 18), 0.95),
                        (at(2026, 9, 5, 10), 0.99)):  # fmt: skip
            _log(s, _sig("ipo", sym, p, when, BLEND_METHOD, "shadow"), "listing_gain", date(2026, 9, 8), 1)
        # no known last bidding day: no decision-time forecast, never the latest by default
        _log(
            s,
            _sig("ipo", unknown, 0.8, at(2026, 9, 3, 12), BLEND_METHOD, "shadow"),
            "listing_gain",
            date(2026, 9, 8),
            1,
        )
        g = next(g for g in ledger.calibration_groups(s, "ipo") if g["method"] == BLEND_METHOD)
    assert g["n"] == 1 and g["mean_p"] == pytest.approx(0.7) and g["brier"] == pytest.approx(0.09)
    cmp = g["ipo_rules"]
    assert cmp["cutoff"]["n"] == 1 and cmp["cutoff"]["brier"] == pytest.approx(0.09)
    # the old rule (latest before listing) on the same issues with a known cut-off: 0.99 → (0.01)²
    assert cmp["latest"]["n"] == 1 and cmp["latest"]["brier"] == pytest.approx(0.0001)
    assert cmp["unknown_close_date"] == 1


# ------------------------------------------------------------------------------------------------ IPO EV after costs
def test_listing_day_selling_costs_golden():
    """Selling ₹18,924.40 of allotted shares on listing day (delivery): STT 0.1 %, NSE 0.00297 %, SEBI ₹10/crore,
    brokerage ₹20 (assumed), DP ₹13 a scrip (assumed), GST 18 % on brokerage + exchange + SEBI + DP."""
    from decimal import Decimal

    from finresearch.fincalc import ipo

    c = ipo.listing_sale_costs(Decimal("18924.40"))
    stt, exch, sebi = 18.9244, 18924.40 * 0.0000297, 18924.40 * 0.000001
    gst = 0.18 * (20 + exch + sebi + 13)
    assert float(c["lines"]["stt"]) == pytest.approx(stt, abs=1e-6)
    assert float(c["lines"]["exchange"]) == pytest.approx(0.5620547, abs=1e-6)
    assert float(c["lines"]["gst"]) == pytest.approx(gst, abs=1e-6)
    assert float(c["total"]) == pytest.approx(stt + exch + sebi + 20 + 13 + gst, abs=1e-6)
    assert float(c["total"]) == pytest.approx(58.5499, abs=1e-3)
    assert {"brokerage", "dp"} <= set(c["assumptions"])  # labelled, never silently 0


async def test_ipo_ev_is_after_listing_day_selling_costs_and_labels_the_allotment_floor():
    from test_ipo_signal import orient, sources

    from finresearch.fincalc import ipo
    from finresearch.signals import ipo as sig_ipo

    s = await sig_ipo.compute("orientcabl", {}, sources(orient(qib="150", retail="20")))
    z = s.sizing
    mean = (18 * 0.30 + 2 * -0.05) / 20  # 0.265
    cost = float(ipo.listing_sale_costs(14960 * (1 + mean))["total"])  # ≈ ₹58.55 on a ₹18,924.40 sale
    assert z["ev_per_lot"] == pytest.approx(0.05 * (14960 * mean - cost)) and z[
        "ev_per_lot"
    ] == pytest.approx(195.2925, abs=0.01)
    assert z["p_allot_is_floor"] is True and "floor" in z["p_allot_basis"]
    assert z["selling_costs_per_lot"] == pytest.approx(cost) and "selling costs" in z["ev_basis"]
