"""portfolio.rebalance: synthetic portfolios, every expected number computed by hand in the comments."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

from finresearch.portfolio.rebalance import Lot, Position, band_pp, fill, plan
from finresearch.portfolio.tax import HoldingTax

TODAY = date(2026, 10, 5)  # FY 2026-27: equity STCG 20 %, LTCG 12.5 % above ₹1.25 lakh, cess 4 %
SLAB = D("0.30")


def pos(hid, name, cls, price, lots, *, asset_type="stock", tax_class="equity", **kw):
    ht = HoldingTax(hid, name, "Demat", f"INE000X0{hid:04d}", tax_class, True, None)
    lots = [Lot(a, D(q), D(c)) for a, q, c in lots]
    value = None if price is None else D(price) * sum((x.quantity for x in lots), D(0))
    return Position(ht, asset_type, cls, value, None if price is None else D(price), lots, **kw)


def debt(hid=9, units=10000):
    return pos(hid, "Example Debt Fund", "Debt funds", 10, [(date(2024, 1, 1), units, 10)], asset_type="mf",
               tax_class="debt_mf")  # fmt: skip


def test_band_is_the_tighter_of_absolute_and_relative():
    assert band_pp(40) == 5  # 25 % of 40 = 10 > 5
    assert band_pp(10) == 2.5  # 25 % of 10
    assert band_pp(0) == 5  # 0 % target: absolute only
    assert band_pp(10, 5, 0) == 5


def test_fill_water_levels_the_largest_shortfalls():
    # total 210; shortfalls B 84, C 34; level L with (84-L)+(34-L)=60 -> L=29: B 55, C 5
    got = fill({"A": D(100), "B": D(0), "C": D(50)}, {"A": 20, "B": 40, "C": 40}, D(60))
    assert got == {"B": D(55), "C": D(5)}
    # cash equal to the shortfalls: total 40, A short 10, B short 20
    got = fill({"A": D(10), "B": D(0)}, {"A": 50, "B": 50}, D(30))  # total 40: A 10, B 20 -> rest 0
    assert got == {"A": D(10), "B": D(20)}


def test_within_bands_no_sells():
    # stocks 52k / debt 48k against 50/50: drift 2 pp < 5 pp
    ps = [pos(1, "Alpha Ltd", "Stocks", 100, [(date(2025, 1, 1), 520, 50)]), debt(units=4800)]
    out = plan(ps, {"Stocks": 50, "Debt funds": 50}, TODAY, [], SLAB)
    assert out["status"] == "within_bands" and out["sells"] == [] and out["buys"] == []
    assert "not investment advice" in out["disclaimer"]


def test_new_money_alone_rebalances_without_sales():
    # stocks 60k / debt 40k, 50/50 targets: 10 pp drift. ₹20k new: total 120k, debt short 20k -> all to debt
    ps = [pos(1, "Alpha Ltd", "Stocks", 100, [(date(2025, 1, 1), 600, 50)]), debt(units=4000)]
    out = plan(ps, {"Stocks": 50, "Debt funds": 50}, TODAY, [], SLAB, new_money=D(20000))
    assert out["status"] == "cash_only"
    assert out["sells"] == []
    assert [(r["asset_class"], r["amount"], r["stamp"]) for r in out["cash_flow"]] == [
        ("Debt funds", 20000.0, 1.0)
    ]
    alloc = {r["label"]: r for r in out["allocation"]}
    assert alloc["Stocks"]["before_pct"] == 60.0 and alloc["Stocks"]["outside_before"]
    assert not alloc["Stocks"]["outside_after_cash"]  # 60000 / 119999 = 50.0004 %


def test_tax_aware_fifo_sell_list_with_exemption_headroom():
    # Stocks 400k of 500k (80 %) against a 20 % target: sell 300k back to 100k
    alpha = pos(1, "Alpha Ltd", "Stocks", 100, [(date(2024, 1, 1), 3000, 20)])  # long-term, +80/unit
    beta = pos(2, "Beta Ltd", "Stocks", 100, [(date(2026, 6, 1), 500, 120)])  # short-term loss, -20/unit
    # oldest lot a short-term gain: FIFO makes the later loss lot unreachable, so Gamma waits behind everything
    gamma = pos(3, "Gamma Ltd", "Stocks", 100, [(date(2026, 8, 1), 200, 80), (date(2026, 9, 1), 300, 150)])
    out = plan([alpha, beta, gamma, debt()], {"Stocks": 20, "Debt funds": 80}, TODAY, [], SLAB)
    assert out["status"] == "rebalance"
    steps = [(s["name"], s["category"], s["units"]) for s in out["sells"]]
    # 1. Beta 500 units (50k). 2. Alpha within the exemption: 1562 units (floor 125000/80) and then, because the
    # mandatory set-off of Beta's loss frees 10,040 of exemption, 125 more (floor 10040/80); 1687 in all.
    # 3. Alpha beyond the exemption: (300000 - 50000 - 168700) / 100 = 813 units.
    assert steps == [("Beta Ltd", "Short-term loss", 500.0),
                     ("Alpha Ltd", "Long-term gain within the exemption", 1687.0),
                     ("Alpha Ltd", "Long-term gain", 813.0)]  # fmt: skip
    b, a1, a2 = out["sells"]
    # Beta: 50,000 gross, STT 0.1 % = 50, gain 49,950 - 60,000 = -10,050; nothing to set it off against yet
    assert (b["gross"], b["charges"], b["gain"], b["tax"]) == (50000.0, 50.0, -10050.0, 0.0)
    # Alpha 1687: 168,700 gross, STT 168.70, cost 33,740 -> gain 134,791.30; less the 10,050 loss = 124,741.30,
    # inside ₹1.25 lakh -> no tax
    assert (a1["gross"], a1["charges"], a1["gain"], a1["tax"]) == (168700.0, 168.7, 134791.3, 0.0)
    # Alpha 813: 81,300 gross, STT 81.30, cost 16,260 -> gain 64,958.70. LTCG 199,750 - 10,050 = 189,700;
    # taxable 64,700 x 12.5 % = 8,087.50 + 4 % cess 323.50 = 8,411.00
    assert (a2["gross"], a2["charges"], a2["gain"], a2["tax"]) == (81300.0, 81.3, 64958.7, 8411.0)
    t = out["totals"]
    assert (t["sold_gross"], t["sell_charges"], t["tax"]) == (300000.0, 300.0, 8411.0)
    assert (t["exemption_before"], t["exemption_after"]) == (125000.0, 0.0)
    # net proceeds 299,700 all go to debt (short 399,760 - 100,000 of the 499,700 total)
    assert [(r["asset_class"], r["amount"]) for r in out["buys"]] == [("Debt funds", 299700.0)]
    alloc = {r["label"]: r for r in out["allocation"]}
    assert alloc["Stocks"]["after_pct"] == 20.01  # 100,000 / 499,685.01
    assert all(s["holding_id"] != 3 for s in out["sells"])


def test_realised_gains_this_year_use_up_the_exemption():
    from finresearch.portfolio.tax import DisposalRow, evaluate

    alpha = pos(1, "Alpha Ltd", "Stocks", 100, [(date(2024, 1, 1), 3000, 20)])
    ht = alpha.holding
    # already booked this FY: an LTCG of exactly 125,000 (sold 1-Jul-2026) -> no headroom left
    done = evaluate(
        DisposalRow(ht, date(2024, 1, 1), date(2026, 7, 1), D(1250), D(0), D(125000), True, "buy")
    )
    out = plan([alpha, debt()], {"Stocks": 50, "Debt funds": 50}, TODAY, [done], SLAB)
    # 300k stocks / 100k debt: sell 100k = 1000 units, all plain LTCG: gain 100,000 - 100 STT - 20,000 = 79,900
    # tax 79,900 x 12.5 % = 9,987.50 x 1.04 = 10,387.00
    (s,) = out["sells"]
    assert (s["category"], s["units"], s["gain"], s["tax"]) == ("Long-term gain", 1000.0, 79900.0, 10387.0)
    assert out["totals"]["exemption_before"] == 0.0


def test_elss_locked_and_exit_load_lots_are_not_sold():
    # Equity funds 40k of 100k against a 10 % target: 30k to sell, but only 20k can be
    elss = pos(4, "Example ELSS Tax Saver Fund", "Equity funds", 100,
               [(date(2022, 1, 10), 100, 50), (date(2024, 6, 1), 100, 60)], asset_type="mf", elss=True)  # fmt: skip
    loaded = pos(5, "Example Flexi Cap Fund", "Equity funds", 100,
                 [(date(2025, 1, 1), 100, 90), (date(2026, 6, 1), 100, 95)], asset_type="mf",
                 exit_load=(D("0.01"), 365))  # fmt: skip
    out = plan([elss, loaded, debt(units=6000)], {"Equity funds": 10, "Debt funds": 90}, TODAY, [], SLAB)
    # both heads are long-term gains inside the exemption; the flexi cap's (+10/unit) is cheaper than the ELSS's
    assert [(s["holding_id"], s["units"]) for s in out["sells"]] == [(5, 100.0), (4, 100.0)]
    # STT 0.001 % on equity fund redemption: 0.10 on 10,000
    assert [(s["charges"], s["gain"], s["tax"]) for s in out["sells"]] == [
        (0.1, 999.9, 0.0),
        (0.1, 4999.9, 0.0),
    ]
    skipped = {s["holding_id"]: s for s in out["skipped"]}
    assert skipped[4]["units"] == 100.0 and "ELSS" in skipped[4]["reason"]  # unlocks 2027-06-02
    assert skipped[5]["units"] == 100.0 and "exit-load" in skipped[5]["reason"]
    (u,) = out["unfilled"]
    assert u["asset_class"] == "Equity funds" and u["amount"] == 10000.0


def test_unpriced_sgb_and_unknown_cost_are_skipped():
    sgb = pos(6, "Example SGB", "Sovereign Gold Bonds", 6000, [(date(2020, 1, 1), 10, 4000)], tax_class="sgb")
    nop = pos(7, "Example Ltd", "Stocks", None, [(date(2020, 1, 1), 10, 100)])
    unk = pos(8, "Delta Ltd", "Stocks", 100, [(date(2020, 1, 1), 10, 100)])
    unk.lots[0].cost_per_unit = None
    out = plan([sgb, nop, unk, debt(units=100)], {"Debt funds": 100}, TODAY, [], SLAB)
    assert out["sells"] == []
    assert {s["holding_id"] for s in out["skipped"]} == {6, 7, 8}
    assert out["unpriced"] == ["Example Ltd"]
