"""Loss set-off and 8-year carry-forward (#344). Synthetic data only; every expected value is hand-computed next to it.

Law: Income-tax Act 1961 s.70(2)/(3) (s.108 of the 2025 Act): STCL against any capital gain, LTCL only against LTCG;
s.74(1) (s.111): unabsorbed capital losses carried forward, STCL against any capital gain, LTCL only against LTCG, for
eight assessment years; s.80 read with s.139(3) (s.121): only if the loss year's return was filed within s.139(1)
time. FY 2025-26 rates (Finance (No.2) Act 2024): equity STCG 20 %, LTCG 12.5 % above ₹1.25 lakh, cess 4 %.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

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
