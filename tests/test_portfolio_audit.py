"""Regression tests from the portfolio audit (Oct-2026). Synthetic data only; every expected number is worked out by
hand in the test's comment, not copied from the code."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from finresearch.portfolio.lots import Event, build_lots

TODAY = date(2026, 9, 30)


# --------------------------------------------------------------------------- lots
def test_sale_without_a_price_is_an_unknown_gain_not_a_total_loss():
    # bought 10 @ ₹100 (cost ₹1,000), sold 10 with no price or amount: the proceeds are unknown, so the gain is too.
    # Proceeds 0 against a cost of ₹1,000 would book a ₹1,000 loss that never happened.
    book = build_lots([Event(1, date(2025, 1, 10), "buy", D(10), D(100)),
                       Event(2, date(2025, 6, 10), "sell", D(10))])  # fmt: skip
    (d,) = book.disposals
    assert d.cost is None and d.gain is None
    assert any("no price or amount" in w for w in book.warnings)


def test_an_older_dividend_does_not_wipe_out_a_statement_opening():
    # a CAS for FY 2024-25 opens with 100 units on 1-Apr-2024; a dividend row dated before it (entered by hand) moves
    # no units, so the opening still stands: 100 units, not 0
    book = build_lots([Event(1, date(2024, 3, 15), "dividend", amount=D(50)),
                       Event(2, date(2024, 4, 1), "opening", D(100), meta={"statement_opening": True})])  # fmt: skip
    assert book.units == D(100)


def _txn(day, kind, qty=None, price=None, amount=None, meta=None, charges=D(0)):
    return SimpleNamespace(day=day, kind=kind, quantity=qty, price=price, amount=amount, charges=charges,
                           meta=meta or {})  # fmt: skip


def test_superseded_opening_is_no_flow_in_xirr_or_value_history():
    """FY 2023-24 and FY 2024-25 statements: the second one's opening (10 units on 1-Apr-2024) is the first one's
    buy, already in the lots. It must not be a second ₹ inflow in the value history, nor block XIRR."""
    from finresearch.portfolio.history import flows_of
    from finresearch.portfolio.valuation import cash_flows

    buy = _txn(date(2023, 6, 10), "buy", D(10), D(100), D(1000))
    opening = _txn(date(2024, 4, 1), "opening", D(10), meta={"statement_opening": True})
    flows, why = cash_flows([buy, opening], D(1500), TODAY)
    assert why is None and flows == [(date(2023, 6, 10), D(-1000)), (TODAY, D(1500))]
    evs = [Event(1, buy.day, "buy", D(10), D(100), D(1000)), Event(2, opening.day, "opening", D(10),
                                                                     meta={"statement_opening": True})]  # fmt: skip
    got = flows_of(evs)
    assert [(f.day, f.cash, f.units) for f in got] == [(date(2023, 6, 10), 1000.0, 0.0)]


def test_xirr_refuses_a_sale_without_an_amount():
    # the 10 units left the holding: without the sale's money XIRR would see ₹1,000 in and nothing back
    flows, why = cash_flows_of([_txn(date(2025, 1, 10), "buy", D(10), D(100), D(1000)),
                                _txn(date(2025, 6, 10), "sell", D(10))])  # fmt: skip
    assert flows == [] and why == "a sale without an amount"


def cash_flows_of(txns):
    from finresearch.portfolio.valuation import cash_flows

    return cash_flows(txns, None, TODAY)


def test_net_invested_counts_a_broker_baseline_at_its_cost():
    """A Groww holdings statement on 30-Sep-2026: 10 units at an average ₹100 become a baseline worth ₹1,000. The
    value includes them, so net invested must too (₹1,000), or the drawdown index reads a ₹1,000 market gain. A
    later buy of 5 @ ₹120 = ₹600 makes it ₹1,600; a statement opening with no cost adds nothing."""
    from finresearch.portfolio.report import Loaded, timeline

    h1, h2 = SimpleNamespace(id=1), SimpleNamespace(id=2)
    txns = {1: [_txn(date(2026, 9, 30), "opening", D(10), D(100), D(1000),
                     {"statement_opening": True, "baseline": True, "cost_basis": "broker_average"}),
                _txn(date(2026, 10, 5), "buy", D(5), D(120), D(600))],
            2: [_txn(date(2026, 9, 1), "opening", D(7), meta={"statement_opening": True})]}  # fmt: skip
    tl = timeline(Loaded([h1, h2], txns, {}, {}))
    assert [(r["date"], r["invested"]) for r in tl] == [("2026-09-01", 1000.0), ("2026-10-01", 1600.0)]


# --------------------------------------------------------------------------- tax
def _holding(fmv=None, tax_class="equity"):
    from finresearch.portfolio.tax import HoldingTax

    return HoldingTax(1, "Example Ltd", "Zerodha", "INE000X01011", tax_class, True, fmv)


@pytest.mark.parametrize(("stt", "tax_cost", "gain"), [(True, D(1500), D(500)), (False, D(1000), D(1000))])
def test_grandfathering_only_for_the_112a_regime(stt, tax_cost, gain):
    """10 shares bought 1-Jun-2017 for ₹1,000, FMV ₹150 on 31-Jan-2018, sold 2-Jun-2025 for ₹2,000.
    With STT (s.112A): cost = max(1,000, min(1,500, 2,000)) = 1,500, gain 500.
    Without STT the sale is taxed under the general rules, where s.55(2)(ac) does not apply: cost 1,000, gain 1,000."""
    from finresearch.portfolio.tax import DisposalRow, evaluate

    r = evaluate(
        DisposalRow(_holding(D(150)), date(2017, 6, 1), date(2025, 6, 2), D(10), D(1000), D(2000), stt, "buy")
    )
    assert r.cls.term == "long" and r.tax_cost == tax_cost and r.gain == gain


def test_intraday_rows_are_not_unknown_capital_gains():
    # one intraday round trip in FY 2025-26: business income, counted as intraday, never as "unknown cost or date"
    from finresearch.portfolio.tax import DisposalRow, evaluate, fy_summary

    r = evaluate(DisposalRow(_holding(), date(2025, 6, 2), date(2025, 6, 2), D(10), D(1000), D(1100), True,
                             "intraday"))  # fmt: skip
    s = fy_summary([r], 2026, D("0.30"))
    assert s["intraday"] == 1 and s["unknown"] == 0 and s["total"] == 0
    assert not any("unknown" in n for n in s["notes"])


# --------------------------------------------------------------------------- importers
def _cas(transactions):
    return {"file_type": "CAMS", "cas_type": "DETAILED", "statement_period": {"from": "01-Apr-2025", "to": "30-Sep-2026"},
            "folios": [{"amc": "Example MF", "folio": "123/45", "schemes": [{
                "scheme": "Example Flexi Cap Fund - Direct Growth", "isin": "INF000X01011", "amfi": "100002",
                "type": "EQUITY", "open": "0", "close": "100", "close_calculated": "100",
                "valuation": {"date": "2026-09-29", "nav": "60", "value": "6000"},
                "transactions": transactions}]}]}  # fmt: skip


def test_cas_stt_is_not_deducted_from_redemption_proceeds():
    """Purchase of 200 units at NAV 50 (₹10,000) with ₹0.50 stamp duty, then a redemption of 100 units at NAV 60 = ₹6,000 with ₹0.06 STT. Stamp duty is a cost of acquisition (charges 0.50);
    STT is not deductible (s.48 fifth proviso): the sale's charges stay 0 and the STT is kept for reference."""
    from finresearch.portfolio.importers import cas_to_result

    res = cas_to_result(_cas([
        {"date": "2025-05-02", "type": "PURCHASE", "units": "200", "nav": "50", "amount": "10000", "description": "P"},
        {"date": "2025-05-02", "type": "STAMP_DUTY_TAX", "amount": "0.50", "description": "*** Stamp Duty ***"},
        {"date": "2026-06-02", "type": "REDEMPTION", "units": "-100", "nav": "60", "amount": "-6000", "description": "R"},
        {"date": "2026-06-02", "type": "STT_TAX", "amount": "0.06", "description": "*** STT Paid ***"},
    ]))  # fmt: skip
    buy, sell = res.txns
    assert (buy.kind, buy.charges) == ("buy", D("0.50"))
    assert (sell.kind, sell.charges, sell.meta["stt"]) == ("sell", D(0), "0.06")
    book = build_lots([Event(1, buy.day, "buy", buy.quantity, buy.price, buy.amount, buy.charges),
                       Event(2, sell.day, "sell", sell.quantity, sell.price, sell.amount, sell.charges)])  # fmt: skip
    (d,) = book.disposals
    # cost = 100 x (10,000.50 / 200) = 5,000.25; proceeds 6,000.00 exactly
    assert d.proceeds == D(6000) and d.cost == D("5000.25")


GROWW_HEAD = ("Stock name,Symbol,ISIN,Type,Quantity,Value,Exchange,Exchange Order Id,Execution date and time,"
              "Order status\n")  # fmt: skip


def test_month_first_tradebook_dates_are_read_as_such():
    # 09-13-2025 can only be 13-Sep-2025, so 09-01-2025 in the same column is 1-Sep-2025 (not 9-Jan-2025)
    from finresearch.portfolio.importers import parse_tradebook

    csv = GROWW_HEAD + ("Example Bank,EXBANK,INE000B01012,BUY,3,3000,NSE,X1,09-01-2025 10:30 AM,Executed\n"
                        "Example Bank,EXBANK,INE000B01012,SELL,1,1100,NSE,X2,09-13-2025 10:30 AM,Executed\n")  # fmt: skip
    res = parse_tradebook(csv.encode(), "groww.csv")
    assert [t.day for t in res.txns] == [date(2025, 9, 1), date(2025, 9, 13)]
    assert any("month-day-year" in w for w in res.warnings)
    # day-first files are unchanged: 13-09-2025 and 01-09-2025
    dmy = GROWW_HEAD + ("Example Bank,EXBANK,INE000B01012,BUY,3,3000,NSE,X1,01-09-2025 10:30 AM,Executed\n"
                        "Example Bank,EXBANK,INE000B01012,SELL,1,1100,NSE,X2,13-09-2025 10:30 AM,Executed\n")  # fmt: skip
    assert [t.day for t in parse_tradebook(dmy.encode(), "g.csv").txns] == [
        date(2025, 9, 1),
        date(2025, 9, 13),
    ]


def test_a_file_mixing_day_first_and_month_first_is_refused():
    from finresearch.portfolio.importers import StatementError, parse_tradebook

    csv = GROWW_HEAD + ("Example Bank,EXBANK,INE000B01012,BUY,3,3000,NSE,X1,13-09-2025 10:30 AM,Executed\n"
                        "Example Bank,EXBANK,INE000B01012,SELL,1,1100,NSE,X2,09-14-2025 10:30 AM,Executed\n")  # fmt: skip
    with pytest.raises(StatementError, match="day-first and month-first"):
        parse_tradebook(csv.encode(), "groww.csv")


# --------------------------------------------------------------------------- dates, prices
def test_statement_day_is_the_ist_calendar_day():
    from finresearch.portfolio.connectors.merge import statement_day

    late = datetime(2026, 9, 30, 20, 30, tzinfo=UTC)  # 02:00 IST on 1-Oct-2026
    assert statement_day("holdings.xlsx", late) == date(2026, 10, 1)
    assert statement_day("Stocks_Holdings_Statement_1_2026-09-28.xlsx", late) == date(2026, 9, 28)
    assert statement_day("Stocks_Holdings_Statement_1_2026-12-28.xlsx", late) == date(
        2026, 10, 1
    )  # never ahead


def test_alert_prices_exclude_a_broker_statement_close():
    from finresearch.portfolio.metrics import cached_prices

    v = {"holdings": {"1": {"price": 100, "price_source": "Groww statement close"},
                      "2": {"price": 50, "price_source": "CAS statement NAV"},
                      "3": {"price": 20, "price_source": "NSE quote: last trade"}}}  # fmt: skip
    assert cached_prices(v) == {3: D(20)}
