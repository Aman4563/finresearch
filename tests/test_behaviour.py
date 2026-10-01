"""Behaviour report (feature #5): the disposition effect exactly as Odean (1998) defines it, Barber & Odean (2000)
turnover, holding periods and the after-sale check, on hand-computed synthetic data (no database, no network)."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal as D

import pytest

from finresearch.portfolio import behaviour as bh
from finresearch.portfolio.lots import Event


def held(name: str, avg: str, price: str | None, comm: str = "0", low: str | None = None) -> bh.Held:
    hi = D(price) if price is not None else None
    lo = D(low) if low is not None else hi
    return bh.Held(name, name, D(avg), D(comm), hi, lo)


def sold(name: str, avg: str, net: str) -> bh.Sold:
    return bh.Sold(name, name, D(avg), D(net))


# --------------------------------------------------------------------------- Odean's own worked example (p. 1782)
def test_odean_worked_example():
    # Investor 1 holds A-E (A, B above cost; C, D, E below) and sells A and C; next day investor 2 holds F, G, H (F, G
    # above; H below) and sells F. Odean: 2 realised gains, 1 realised loss, 2 paper gains, 3 paper losses (the
    # paper's text says "D, E, and G are paper losses"; G is above cost, so it means H, as its totals show).
    d1 = bh.SaleDay(date(2026, 1, 5), 5, held=[held("B", "100", "110"), held("D", "100", "90"), held("E", "100", "80")],
                    sold=[sold("A", "100", "120"), sold("C", "100", "95")])  # fmt: skip
    d2 = bh.SaleDay(date(2026, 1, 6), 3, held=[held("G", "50", "55"), held("H", "50", "45")],
                    sold=[sold("F", "50", "60")])  # fmt: skip
    r = bh.disposition([d1, d2])
    assert (r["realised_gains"], r["realised_losses"], r["paper_gains"], r["paper_losses"]) == (2, 1, 2, 3)
    assert r["pgr"] == pytest.approx(2 / 4)  # 0.5
    assert r["plr"] == pytest.approx(1 / 4)  # 0.25
    # footnote 6: SE = sqrt(0.5·0.5/4 + 0.25·0.75/4) = sqrt(0.0625 + 0.046875) = 0.330719
    assert r["se"] == pytest.approx(0.330719, abs=1e-6)
    assert r["t"] == pytest.approx(0.25 / 0.330719, abs=1e-5)  # 0.75593
    assert r["difference"] == pytest.approx(0.25)
    assert r["ratio"] == pytest.approx(2.0)


def test_straddle_single_stock_and_no_sale_days():
    # the average purchase price between the day's low and high: neither; a day with one stock: skipped
    straddle = bh.SaleDay(
        date(2026, 2, 2), 2, held=[held("X", "100", "102", low="98")], sold=[sold("Y", "10", "11")]
    )
    single = bh.SaleDay(date(2026, 2, 3), 1, sold=[sold("Z", "10", "5")])
    empty = bh.SaleDay(date(2026, 2, 4), 4)
    r = bh.disposition([straddle, single, empty])
    assert (r["realised_gains"], r["paper_gains"], r["paper_losses"], r["realised_losses"]) == (1, 0, 0, 0)
    assert r["sale_days"] == 1 and r["single_stock_days"] == 1
    assert r["days"][0]["neither"] == ["X"]
    assert r["plr"] is None and r["t"] is None
    # a sale exactly at the average price is neither a gain nor a loss
    assert bh.realised_status(sold("A", "100", "100")) == "neither"


def test_commission_is_deducted_for_paper_positions():
    # close 100.5 is above the 100 average, but less the 1.0 average buy commission it is below: a paper loss
    assert bh.paper_status(held("A", "100", "100.5", comm="1")) == "loss"
    assert bh.paper_status(held("A", "100", "101.5", comm="1")) == "gain"
    assert bh.paper_status(held("A", "100", None)) == "unpriced"
    assert bh.paper_status(bh.Held("A", "A", None, D(0), D(1), D(1))) == "unknown_cost"


# --------------------------------------------------------------------------- from transactions
def ev(i: int, day: str, kind: str, q: str | None = None, price: str | None = None, charges: str = "0",
       **meta) -> Event:  # fmt: skip
    qd = D(q) if q else None
    amount = qd * D(price) if q and price else None
    return Event(
        i, date.fromisoformat(day), kind, qd, D(price) if price else None, amount, D(charges), True, meta
    )


def test_sale_days_from_transactions_hand_computed():
    A = bh.Group("A", "Example Alpha", "stock", [[ev(1, "2026-01-05", "buy", "10", "100", "10"),
                                                  ev(2, "2026-02-02", "sell", "4", "101.5", "4"),
                                                  ev(3, "2026-03-10", "sell", "1", "90")]])  # fmt: skip
    B = bh.Group("B", "Example Beta", "stock", [[ev(4, "2026-01-05", "buy", "5", "200"),
                                                 ev(5, "2026-02-10", "sell", "5", "190")]])  # fmt: skip
    C = bh.Group("C", "Example Gamma", "stock", [[ev(6, "2026-01-06", "buy", "10", "50", "5"),
                                                  ev(7, "2026-03-02", "sell", "10", "60")]])  # fmt: skip
    Dg = bh.Group("D", "Example Delta", "stock", [[ev(8, "2026-03-02", "buy", "5", "10"),
                                                   ev(9, "2026-03-02", "sell", "5", "11")]])  # fmt: skip
    prices = {("B", date(2026, 2, 2)): ("210", "210"), ("C", date(2026, 2, 2)): ("50.8", "50.8"),
              ("A", date(2026, 2, 10)): ("105", "105"), ("C", date(2026, 2, 10)): ("51.5", "50.6"),
              ("A", date(2026, 3, 2)): ("95", "95")}  # fmt: skip

    def price_of(k, d):
        p = prices.get((k, d))
        return (D(p[0]), D(p[1])) if p else None

    days = bh.sale_days([A, B, C, Dg], date(2026, 1, 1), date(2026, 3, 31), price_of)
    by = {d.day: d for d in days}
    # A: cost (1000 + 10) / 10 = 101 incl. a 1.0/unit commission; the 2-Feb sale nets (406 - 4) / 4 = 100.5 < 101
    a = by[date(2026, 2, 2)].sold[0]
    assert (a.avg_cost, a.net_price) == (D("101"), D("100.5"))
    c = next(h for h in by[date(2026, 2, 2)].held if h.key == "C")
    assert (c.avg_cost, c.commission) == (D("50.5"), D("0.5"))
    # 2-Mar: D was bought and sold that day, so it was not in the portfolio at the start of the day
    assert by[date(2026, 3, 2)].n_stocks == 2 and [s.key for s in by[date(2026, 3, 2)].sold] == ["C"]
    assert by[date(2026, 3, 10)].n_stocks == 1  # only A left: skipped
    r = bh.disposition(days)
    # 2-Feb: A realised loss (commission), B paper gain (210 > 200), C paper loss (50.8 - 0.5 < 50.5)
    # 10-Feb: B realised loss (190 < 200), A paper gain (105 - 1 > 101), C neither (51.0 vs 50.1 around 50.5)
    # 2-Mar: C realised gain (60 > 50.5), A paper loss (95 - 1 < 101)
    assert (r["realised_gains"], r["realised_losses"], r["paper_gains"], r["paper_losses"]) == (1, 2, 2, 2)
    assert r["sale_days"] == 3 and r["single_stock_days"] == 1
    assert r["pgr"] == pytest.approx(1 / 3) and r["plr"] == pytest.approx(0.5)
    se = math.sqrt((1 / 3) * (2 / 3) / 3 + 0.5 * 0.5 / 4)  # sqrt(0.074074 + 0.0625) = 0.369559
    assert r["se"] == pytest.approx(0.369559, abs=1e-6) == pytest.approx(se)
    assert r["t"] == pytest.approx(-0.45099, abs=1e-5)


def test_split_on_the_sale_day_adjusts_the_reference_price():
    # bought 10 at 101 (incl. charges); a 10 -> 2 face-value split (5 for 1) on the day: the reference is 101 / 5
    A = bh.Group("A", "A", "stock", [[ev(1, "2026-01-05", "buy", "10", "100", "10"),
                                      ev(2, "2026-02-02", "split", **{"from": "10", "to": "2"})]])  # fmt: skip
    units, avg, comm = bh.position_at(A, date(2026, 2, 2))
    assert (units, avg, comm) == (D(50), D("20.2"), D("0.2"))


# --------------------------------------------------------------------------- turnover
def test_turnover_barber_odean_hand_computed():
    X = bh.Group(
        "X",
        "X",
        "stock",
        [[ev(1, "2026-01-10", "buy", "100", "10"), ev(2, "2026-02-12", "sell", "40", "13")]],
    )
    Y = bh.Group("Y", "Y", "mf", [[ev(3, "2026-01-15", "buy", "50", "20")]])
    closes = {"X": {date(2026, 1, 30): D(12)}, "Y": {date(2026, 1, 30): D(20)}}

    def before(k, d):
        return closes[k].get(date(2026, 1, 30)) if d == date(2026, 2, 1) else None

    t = bh.turnover([X, Y], date(2026, 2, 1), date(2026, 3, 15), before)
    feb = t["months"][0]
    # BOM value 100·12 + 50·20 = 2200; sales 40·12 = 480; January's purchases still held: 1200 + 1000 = 2200
    assert feb["value"] == 2200.0
    assert feb["sales"] == pytest.approx(480 / 2200)  # 0.218182
    assert feb["purchases"] == pytest.approx(1.0)
    assert feb["turnover"] == pytest.approx((480 / 2200 + 1) / 2)  # 0.609091
    assert len(t["months"]) == 1  # March is not over on 15-Mar
    assert t["annual"] == pytest.approx(0.609091 * 12, abs=1e-5)
    assert t["annual_sales"] == pytest.approx(480 / 2200 * 12)


# --------------------------------------------------------------------------- holding periods, after the sale
def test_weighted_median_and_holding_periods():
    assert bh.weighted_median([(10, 1), (20, 1), (30, 2)]) == 20
    assert bh.weighted_median([(10, 3), (100, 1)]) == 10
    assert bh.weighted_median([]) is None
    rows = [bh.Realised("W", date(2026, 1, 1), date(2026, 1, 31), D(10), D(100), D(150), "buy"),  # +, 30 days
            bh.Realised("L", date(2025, 1, 1), date(2026, 1, 1), D(10), D(100), D(50), "buy"),  # -, 365 days
            bh.Realised("I", date(2026, 1, 2), date(2026, 1, 2), D(1), D(1), D(2), "intraday")]  # fmt: skip
    opn = [("O1", date(2025, 10, 1), D(5), D(100), D(80)), ("O2", date(2026, 3, 1), D(5), D(100), D(120)),
           ("O3", None, D(1), None, D(1))]  # fmt: skip
    r = bh.holding_periods(rows, opn, date(2026, 4, 1))
    assert r["realised"]["winners"]["median_days"] == 30 and r["realised"]["losers"]["median_days"] == 365
    assert (
        r["realised"]["skipped"] == 1 and r["realised"]["reading"] == "winners were sold sooner than losers"
    )
    assert r["open"]["in_loss"]["median_days"] == 182 and r["open"]["in_profit"]["median_days"] == 31
    assert r["open"]["unknown"] == 1


def test_forward_return_needs_the_full_horizon():
    rets = {date(2026, 1, d): 0.01 for d in range(2, 6)}
    assert bh.forward_return(rets, date(2026, 1, 1), n=3) == pytest.approx(1.01**3 - 1)
    assert bh.forward_return(rets, date(2026, 1, 3), n=3) is None


def test_period_return_and_fy_rows():
    days = [date(2026, 3, 31), date(2026, 4, 1), date(2026, 9, 30)]
    idx = [1.0, 1.1, 1.21]
    assert bh.period_return(days, idx, date(2026, 3, 31), date(2026, 9, 30)) == pytest.approx(0.21)
    rows = bh.frequency_vs_returns([(date(2026, 5, 1), "buy", "X")], date(2026, 4, 1), date(2026, 9, 30), days, idx,
                                   [1.0, 1.0, 1.1], [])  # fmt: skip
    assert rows[0]["label"] == "FY 2026-27" and rows[0]["fy"] == 2027 and rows[0]["trades"] == 1
    assert rows[0]["twr"] == pytest.approx(0.21) and rows[0]["excess"] == pytest.approx(0.11)
