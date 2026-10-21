"""Loss set-off and 8-year carry-forward (#344). Synthetic data only; every expected value is hand-computed next to it.

Law: Income-tax Act 1961 s.70(2)/(3) (s.108 of the 2025 Act): STCL against any capital gain, LTCL only against LTCG;
s.74(1) (s.111): unabsorbed capital losses carried forward, STCL against any capital gain, LTCL only against LTCG, for
eight assessment years; s.80 read with s.139(3) (s.121): only if the loss year's return was filed within s.139(1)
time. FY 2025-26 rates (Finance (No.2) Act 2024): equity STCG 20 %, LTCG 12.5 % above ₹1.25 lakh, cess 4 %.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

from finresearch.fincalc.losses import ManualLoss, YearGains, carry_forward, last_fy
from finresearch.fincalc.tax import BroughtForward, Gain, classify, fy_tax

SOLD = date(2025, 10, 1)  # FY 2025-26 (fy 2026)


def _g(amount, acquired, sold=SOLD, cls="equity", **kw):
    return Gain(D(amount), classify(cls, acquired, sold, **kw), sold)


LTCG = date(2020, 1, 1)  # held > 12 months at SOLD
STCG = date(2025, 6, 1)  # held < 12 months at SOLD


# --------------------------------------------------------------------------- brought-forward stage of fy_tax
def test_brought_forward_stcl_against_stcg():
    # STCG 1,00,000 at 20 %; b/f STCL 60,000 -> 40,000 × 20 % = 8,000; cess 320
    t = fy_tax(
        [_g(100_000, STCG)], 2026, "0.30", brought_forward=[BroughtForward(2024, "short", D(60_000), "a")]
    )
    assert (t.tax, t.cess) == (D("8000.00"), D("320.00"))
    assert t.bf_used == {"a": D(60_000)}
    assert (t.bf_set_off_short, t.bf_set_off_long) == (D(60_000), D(0))


def test_brought_forward_ltcl_never_against_stcg():
    # STCG 1,00,000 and LTCG 50,000; b/f LTCL 2,00,000 absorbs only the LTCG (s.74(1)(b)); STCG 20,000 tax; 1,50,000 left
    t = fy_tax([_g(100_000, STCG), _g(50_000, LTCG)], 2026, "0.30",
               brought_forward=[BroughtForward(2024, "long", D(200_000), "l")])  # fmt: skip
    assert t.bf_used == {"l": D(50_000)}
    assert t.tax == D("20000.00") and t.exemption_used == 0


def test_exemption_applies_after_brought_forward_set_off():
    # LTCG 3,00,000; b/f STCL 1,00,000 -> 2,00,000; exempt 1,25,000; 75,000 × 12.5 % = 9,375
    t = fy_tax([_g(300_000, LTCG)], 2026, brought_forward=[BroughtForward(2023, "short", D(100_000), "s")])
    assert t.tax == D("9375.00") and t.exemption_used == D(125_000)


def test_brought_forward_set_off_is_compulsory_even_against_exempt_ltcg():
    # s.74(1)(a) "shall be set off": LTCG 1,00,000 (all inside the exemption) still absorbs b/f STCL 50,000
    t = fy_tax([_g(100_000, LTCG)], 2026, brought_forward=[BroughtForward(2023, "short", D(50_000), "s")])
    assert t.bf_used == {"s": D(50_000)} and t.exemption_used == D(50_000) and t.tax == 0


def test_current_year_losses_go_first_then_oldest_brought_forward():
    # STCG 1,00,000, current STCL 30,000 (s.70 first) -> 70,000; b/f STCL fy2021 40,000 then fy2023 50,000 (30,000 used)
    t = fy_tax([_g(100_000, STCG), _g(-30_000, date(2025, 5, 1))], 2026,
               brought_forward=[BroughtForward(2023, "short", D(50_000), "new"),
                                BroughtForward(2021, "short", D(40_000), "old")])  # fmt: skip
    assert t.bf_used == {"old": D(40_000), "new": D(30_000)} and t.tax == 0


def test_brought_forward_ltcl_is_applied_before_stcl():
    # only LTCG 3,00,000 (175,000 taxable after the exemption): LTCL can only use LTCG, STCL could wait for any gain,
    # so LTCL goes first (convention: maximises what is absorbed). LTCL 2,00,000 used, STCL 1,00,000 used
    t = fy_tax([_g(300_000, LTCG)], 2026,
               brought_forward=[BroughtForward(2020, "short", D(100_000), "s"),
                                BroughtForward(2022, "long", D(250_000), "l")])  # fmt: skip
    assert t.bf_used == {"l": D(250_000), "s": D(50_000)}
    assert t.tax == 0 and t.losses_unabsorbed_short == 0  # current-year figures unchanged by b/f


# --------------------------------------------------------------------------- the ledger (fincalc.losses)
def _yg(*gains, complete=True, maybe_loss=False):
    return YearGains(list(gains), complete=complete, maybe_loss=maybe_loss)


FY25_LTCL = _g(-50_000, date(2019, 1, 1), date(2024, 10, 1))  # FY 2024-25 (fy 2025) LTCL
FY26_LTCG = _g(200_000, LTCG)  # FY 2025-26 (fy 2026) LTCG


def test_last_fy_is_eight_assessment_years_after_the_loss_year():
    # s.74(2): loss of FY 2017-18 (AY 2018-19) may be set off up to AY 2026-27 = FY 2025-26 (fy 2026)
    assert last_fy(2018) == 2026


def test_computed_loss_chains_into_the_next_year_when_filed_on_time():
    led = carry_forward({2025: _yg(FY25_LTCL), 2026: _yg(FY26_LTCG)}, "0.30", filed={2025: True})
    y = led.years[2026]
    # LTCG 2,00,000 - b/f LTCL 50,000 = 1,50,000 - 1,25,000 = 25,000 × 12.5 % = 3,125
    assert y.tax.tax == D("3125.00") and y.tax.bf_set_off_long == D(50_000) and y.carry_forward_complete
    (e,) = led.entries
    assert (e.origin_fy, e.kind, e.amount, e.remaining, e.last_fy, e.status) == (
        2025,
        "long",
        D(50_000),
        0,
        2033,
        "used",
    )
    assert e.used == {2026: D(50_000)} and e.source == "computed"


def test_late_return_means_the_loss_is_not_carried():
    led = carry_forward({2025: _yg(FY25_LTCL), 2026: _yg(FY26_LTCG)}, "0.30", filed={2025: False})
    # 2,00,000 - 1,25,000 = 75,000 × 12.5 % = 9,375
    assert led.years[2026].tax.tax == D("9375.00") and led.years[2026].carry_forward_complete
    assert led.entries[0].status == "not_carried" and led.entries[0].remaining == D(50_000)


def test_unknown_filing_is_never_assumed():
    led = carry_forward({2025: _yg(FY25_LTCL), 2026: _yg(FY26_LTCG)}, "0.30")
    y = led.years[2026]
    assert y.tax.tax == D("9375.00")  # the loss is not applied: lower tax is the unsafe direction
    assert not y.carry_forward_complete and y.tax_if_pending_allowed == D("3250.00")  # 3,125 + 4 % cess
    assert led.entries[0].status == "unknown_filing" and led.entries[0].remaining == D(50_000)


def test_return_not_yet_due_is_its_own_status():
    # FY 2025-26 loss; on 10-May-2026 the s.139(1) date (31-Jul-2026 for a non-audit individual) has not passed
    led = carry_forward({2026: _yg(_g(-10_000, STCG))}, "0.30", today=date(2026, 5, 10), through_fy=2027)
    assert led.entries[0].status == "not_yet_due"
    led = carry_forward({2026: _yg(_g(-10_000, STCG))}, "0.30", today=date(2026, 8, 1), through_fy=2027)
    assert led.entries[0].status == "unknown_filing"


def test_manual_loss_used_on_the_eighth_year_and_lapsed_on_the_ninth():
    gains = {2026: _yg(_g(100_000, STCG))}
    ok = carry_forward(
        gains, "0.30", manual=[ManualLoss(2018, "short", D(40_000), "ITR AY 2018-19")], filed={2018: True}
    )
    assert ok.years[2026].tax.tax == D("12000.00") and ok.entries[0].status == "used"  # 60,000 × 20 %
    old = carry_forward(
        gains, "0.30", manual=[ManualLoss(2017, "short", D(40_000), "ITR AY 2017-18")], filed={2017: True}
    )
    assert old.years[2026].tax.tax == D("20000.00") and old.entries[0].status == "lapsed"
    assert old.years[2026].carry_forward_complete


def test_manual_loss_applies_from_the_stated_year_and_tracks_what_is_left():
    m = ManualLoss(2022, "short", D(150_000), "ITR AY 2024-25 Schedule CFL", apply_from_fy=2025)
    led = carry_forward({2024: _yg(_g(100_000, date(2023, 6, 1), date(2023, 10, 1))), 2026: _yg(_g(100_000, STCG))},
                        "0.30", manual=[m], filed={2022: True})  # fmt: skip
    assert (
        led.years[2024].tax.bf_set_off_short == 0
    )  # before apply_from_fy: already reflected in the entered amount
    assert led.entries[0].used == {2026: D(100_000)} and led.entries[0].remaining == D(50_000)
    assert led.entries[0].status == "available" and led.entries[0].source == "ITR AY 2024-25 Schedule CFL"


def test_incomplete_origin_year_gives_an_unknown_amount():
    led = carry_forward({2025: _yg(FY25_LTCL, complete=False, maybe_loss=True), 2026: _yg(FY26_LTCG)}, "0.30",
                        filed={2025: True})  # fmt: skip
    kinds = {(e.kind, e.status, e.amount) for e in led.entries}
    assert kinds == {("long", "unknown_amount", None), ("unknown", "unknown_amount", None)}
    y = led.years[2026]
    assert y.tax.tax == D("9375.00") and not y.carry_forward_complete and y.tax_if_pending_allowed is None


def test_manual_entry_replaces_the_computed_loss_of_the_same_year_and_kind():
    m = ManualLoss(2025, "long", D(30_000), "ITR AY 2025-26 Schedule CFL")
    led = carry_forward({2025: _yg(FY25_LTCL), 2026: _yg(FY26_LTCG)}, "0.30", manual=[m], filed={2025: True})
    (e,) = led.entries
    assert e.amount == D(30_000) and e.source.startswith("ITR")
    assert any("50,000" in n for n in e.notes)  # the app's own figure is shown for reconciliation
    assert led.years[2026].tax.bf_set_off_long == D(30_000)


def test_late_return_wins_over_an_unknown_amount():
    led = carry_forward(
        {2025: _yg(FY25_LTCL, complete=False), 2026: _yg(FY26_LTCG)}, "0.30", filed={2025: False}
    )
    assert [e.status for e in led.entries] == ["not_carried"] and led.years[2026].carry_forward_complete
