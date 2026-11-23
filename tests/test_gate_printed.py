"""Printed-value check of the publish gate (#502): the figure beside a citation is the cited claim's value.

Expected verdicts are worked out by hand from the rule in `verify.printed`'s docstring. The report lines are either
planted mismatches (the issue's list) or the hard layouts of live reports: several claims in one table row, a
"Source" column, reversed pairs, computed figures with their inputs, times and dates next to a citation, ranges.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from finresearch.verify.printed import check_printed, figures, matches

EVAL = Path(__file__).parent / "fixtures" / "eval"


def claim(cid, value, unit=None, *, statement="", metric="m", period="FY2026", importance="normal",
          claim_type="numeric"):  # fmt: skip
    """A ledger row as the check reads it (the attributes of `db.models.Claim` it uses)."""
    return SimpleNamespace(id=cid, value=Decimal(str(value)) if value is not None else None, unit=unit,
                           statement=statement, metric=metric, period=period, importance=importance,
                           claim_type=claim_type)  # fmt: skip


def run(report, *claims):
    return check_printed(report, {c.id: c for c in claims})


def flagged(report, *claims):
    return [(f.claim_id, f.kind, f.printed) for f in run(report, *claims).findings]


def fig(text):
    (f,) = figures(text)
    return f


PAT = claim(4804, "2651.97", "INR million", metric="pat", statement="PAT for FY2026 was ₹2,651.97 million.")


# ------------------------------------------------------------------ reading the figures a report prints
def test_dates_times_period_labels_and_codes_are_not_figures():
    # a 4-digit year inside a date stays readable (a claim may hold a year); days, months and times do not
    dated = "on 29-Sep-2026 at 16:09 IST, 17:00:00, 30-Jun-26, 29 Sep, 2026-09-28 and 28/09/2026"
    assert [f.value for f in figures(dated)] == [2026, 2026] and not any(f.figure for f in figures(dated))
    periods = "FY26, FY24–26, FY2020-21, 2025-26, Q1 FY27, H1, Jan-2025 and March 31, 2025"
    assert [f.value for f in figures(periods)] == [2025, 2025]
    assert [
        f.value for f in figures("CAT6A cable, the 1Lattice report, ISIN INE027E07998, 898LTF29 N7, 5G")
    ] == []
    assert [f.value for f in figures("see [C4798] and [C12]")] == []


def test_markers_scales_and_indian_grouping_are_read():
    f = fig("₹1,94,805")
    assert (f.value, f.cur, f.scale, f.figure) == (Decimal("194805"), "INR", None, True)
    assert (fig("₹98.80m").scale, fig("₹35.9bn").scale, fig("₹2 crore").scale) == (10**6, 10**9, 10**7)
    assert (fig("₹2.65k crore").base, fig("₹2 lakh crore").base) == (Decimal("2.65e10"), Decimal("2e12"))
    assert (fig("43.90 lakh shares").base, fig("43.90 lakh shares").cur) == (Decimal("4390000"), None)
    assert (fig("US$20,158 mn").cur, fig("Rs. 258").cur, fig("40.35x").kind) == ("USD", "INR", "mult")
    assert (fig("65.42%").kind, fig("0.2 pp").kind, fig("17.37 times").kind) == ("pct", "pct", "mult")
    assert fig("8 funds").figure is False and fig("8 funds").word == "funds"  # a bare integer, not a figure
    assert [f.value for f in figures("895,776 km and 500m of cable")] == [Decimal("895776")]


def test_ranges_hyphens_and_brackets_are_not_minus_signs():
    assert [(f.value, f.neg) for f in figures("29.84x–43.99x, ₹258–₹272, top-10, Day-2, 10 - 12")] == [
        (Decimal("29.84"), False), (Decimal("43.99"), False), (Decimal("258"), False), (Decimal("272"), False),
        (Decimal("10"), False), (Decimal("2"), False), (Decimal("10"), False), (Decimal("12"), False)]  # fmt: skip
    assert [(f.neg, f.plus) for f in figures("−9.93% to +21.32%, -₹120, ₹-130")] == [
        (True, False), (False, True), (True, False), (True, False)]  # fmt: skip
    # a bracket is an accounting negative when it is the whole table cell, or wraps the number after the ₹ sign
    assert (
        figures("(117.64)", cell=True)[0].neg and fig("₹(117.64)m").neg and fig("₹(117.64)m").scale == 10**6
    )
    assert [f.neg for f in figures("5,490.58 (83.47%)", cell=True)] == [False, False]  # a share, in brackets
    assert not fig("EPS (₹5.27)").neg and not figures("(117.64)")[0].neg  # prose: a parenthesis


# ------------------------------------------------------------------ the rounding rule
@pytest.mark.parametrize("printed,ok", [
    ("₹2,651.97 crore", True), ("₹2,652 crore", True), ("₹2,652.0 crore", True), ("₹2.65k crore", True),
    ("₹26.5bn", True), ("₹26.52 billion", True), ("₹26,519.7m", True), ("2,651.97", True), ("2,652", True),
    # not the value rounded to the printed digit: truncated, a digit off, digits transposed
    ("₹2,651 crore", False), ("₹2,653 crore", False), ("₹2.66k crore", False), ("₹26.6bn", False),
    ("₹2,561.97 crore", False), ("2,651.79", False),
    # a scale word is binding: the same digits in lakh or million are another amount
    ("₹2,651.97 lakh", False), ("₹2,651.97m", False), ("₹2,651.97 million", False),
])  # fmt: skip
def test_display_rounding_in_the_unit_the_figure_names(printed, ok):
    """2,651.97 crore = 26.5197 bn = 26,519.7 m. Half a unit of the last printed digit either way, in the printed
    unit: "2.65k crore" covers 2,645-2,655 crore, "26.5bn" covers 26.45-26.55 bn."""
    assert matches(fig(printed), Decimal("2651.97"), "INR crore") is ok


@pytest.mark.parametrize("printed,value,unit,ok", [
    ("3 plants", "2.65", "x", False),  # a bare integer below 100 is a count, never a rounded 2.65
    ("3", "2651.97", "INR million", False),  # nor the claim's 2.65 billion
    ("2.7", "2651.97", "INR million", False),  # an unmarked figure is another ₹ scale only within REL_TOL
    ("265.20", "2651.97", "INR million", True),  # 265.197 crore, printed in a crore table without the word
    ("26,519.7", "2651.97", "INR million", True),  # the lakh form
    ("889", "888.84", "INR/share", True), ("6 months", "6.2", "months", True),  # its own unit word follows
    ("6 funds", "6.2", "months", False), ("3", "3", "count", True), ("0.06x", "0.057377", "x", True),
    ("0.057x", "0.057377", "x", True), ("0.058x", "0.057377", "x", False), ("40x", "40.35", "x", True),
    ("41x", "40.35", "x", False), ("1.80x", "1.8", "x", True), ("0%", "15", "%", False), ("0", "15", "%", False),
    ("0.00", "0", "INR million", True), ("43.90 lakh shares", "4390000", "shares", True),
    ("$5.1 billion", "5100", "USD million", True), ("₹5,100 million", "5100", "USD million", False),
])  # fmt: skip
def test_rounding_never_reaches_a_bare_count_a_zero_or_an_unnamed_scale(printed, value, unit, ok):
    assert matches(figures(printed)[0], Decimal(value), unit) is ok


@pytest.mark.parametrize("printed,value,unit,ok", [
    ("15%", "15", "%", True), ("15.0%", "15", "%", True), ("0.15", "15", "%", True), ("0.15%", "15", "%", False),
    ("1500%", "15", "%", False), ("15%", "0.15", "fraction", True), ("0.15", "0.15", "fraction", True),
    ("15", "0.15", "fraction", True), ("0.15%", "0.15", "fraction", False), ("20.1%", "-0.2006", "fraction", True),
    ("0.96x", "96", "%", True), ("65.42%", "65.42", "x", False), ("₹65.42", "65.42", "%", False),
])  # fmt: skip
def test_a_percent_sign_is_binding_and_an_unmarked_figure_may_be_the_fraction(printed, value, unit, ok):
    assert matches(fig(printed), Decimal(value), unit) is ok


# ------------------------------------------------------------------ planted mismatches (the issue's list)
REV = claim(4803, "11716.54", "INR million", metric="revenue",
            statement="Revenue from operations was ₹11,716.54 million in FY2026.")  # fmt: skip


def test_transposed_digits_name_the_claim_the_value_held_and_the_figure_printed():
    rep = run("FY26 PAT was ₹2,561.97m [C4804], up on the year.", PAT)
    (f,) = rep.findings
    assert (f.claim_id, f.kind, f.printed, f.line, f.importance) == (4804, "value", "₹2,561.97m", 1, "normal")
    msg = f.message()
    assert (
        "[C4804] holds 2,651.97 INR million (pat, FY2026) but the report prints ₹2,561.97m beside it" in msg
    )
    assert msg.endswith('line 1: "FY26 PAT was ₹2,561.97m [C4804]"')
    assert rep.outcomes["mismatch"] == rep.judged == 1
    assert flagged("FY26 PAT was ₹2,651.97m [C4804], up on the year.", PAT) == []


@pytest.mark.parametrize("printed", ["₹2,651.97 crore", "₹2,651.97 lakh", "₹2,651.97bn"])
def test_the_same_digits_under_another_rupee_scale_are_a_scale_mismatch(printed):
    assert flagged(f"PAT was {printed} [C4804].", PAT) == [(4804, "scale", printed)]


def test_another_scale_with_the_right_amount_passes_and_the_wrong_amount_does_not():
    for ok in ("₹265.2 crore", "₹2.652bn", "₹26,519.7 lakh", "₹2,652 million", "₹2,651.97m"):
        assert flagged(f"PAT was {ok} [C4804].", PAT) == [], ok
    assert flagged("PAT was ₹26.52 crore [C4804].", PAT) == [
        (4804, "value", "₹26.52 crore")
    ]  # = 265.2 million


LOSS = claim(
    11, "-120.50", "INR million", metric="pat", statement="The company made a loss of ₹(120.50) million."
)


def test_a_loss_printed_as_a_profit_is_a_sign_mismatch():
    assert flagged("The company made a profit of ₹120.50m in FY26 [C11].", LOSS) == [(11, "sign", "₹120.50m")]
    assert flagged("PAT was +₹120.50m [C11].", LOSS) == [(11, "sign", "+₹120.50m")]
    for ok in ("It made a loss of ₹120.50m [C11].", "PAT was ₹(120.50)m [C11].", "PAT −₹120.50m [C11].",
               "| PAT | (120.50) [C11] |"):  # fmt: skip
        assert flagged(ok, LOSS) == [], ok


def test_a_negative_claim_printed_without_a_sign_word_is_not_evidence_of_the_other_sign():
    """Live run 5: the claim holds -348.45 (books below the bank returns); the report says "higher than the books"."""
    caro = claim(555, "-348.45", "INR million", metric="caro_receivables_mismatch",
                 statement="Receivables reported to banks exceeded the books by (348.45)m at 31-Mar-2026.")  # fmt: skip
    assert flagged("Receivables reported to banks were ₹348.45m higher than the books [C555].", caro) == []
    shrink = claim(
        1587, "-2.32", "%", metric="buyback_share_shrink_yoy", statement="Share count fell 2.32% YoY."
    )
    # "growth" belongs to the clause before the bracket (live run 9)
    assert flagged("Mid-single-digit EPS growth (share-count shrink of 2.32% [C1587]).", shrink) == []
    assert flagged("The share count grew 2.32% [C1587].", shrink) == [(1587, "sign", "2.32%")]


def test_a_profit_or_a_rate_printed_negative_is_a_sign_mismatch_but_an_outflow_row_is_not():
    profit = claim(
        12, "535.61", "INR million", metric="pat", statement="Profit after tax was ₹535.61 million."
    )
    assert flagged("It made a loss of ₹535.61m [C12].", profit) == [(12, "sign", "₹535.61m")]
    assert flagged("| PAT | (535.61) [C12] |", profit) == [(12, "sign", "(535.61)")]
    margin = claim(14, "8.23", "%", metric="ebitda_margin", statement="EBITDA margin was 8.23% in FY2026.")
    assert flagged("EBITDA margin was −8.23% [C14].", margin) == [(14, "sign", "−8.23%")]
    capex = claim(
        13, "635.75", "INR million", metric="capex", statement="Capex was ₹635.75 million in FY2026."
    )
    assert flagged("| Purchase of equipment | (635.75) [C13] |", capex) == []  # negative by convention


def test_per_cent_and_fraction_confused():
    roe = claim(21, "15", "%", metric="roe", statement="ROE was 15% in FY2026.")
    assert flagged("ROE of 0.15% [C21].", roe) == [(21, "percent", "0.15%")]
    assert flagged("ROE of 1,500% [C21].", roe) == [(21, "value", "1,500%")]
    drawdown = claim(22, "0.2006", "fraction", metric="max_drawdown", statement="Max drawdown is 20.06%.")
    assert flagged("A drawdown of 0.2006% [C22].", drawdown) == [(22, "percent", "0.2006%")]
    for ok in ("ROE of 15% [C21].", "ROE of 15.0% [C21].", "ROE 0.15 [C21]."):
        assert flagged(ok, roe) == [], ok
    assert flagged("A drawdown of 20.06% [C22], or 0.2006 [C22].", drawdown) == []


def test_the_right_number_beside_the_wrong_claim_id():
    assert flagged("PAT was ₹2,651.97m [C4803].", REV, PAT) == [(4803, "value", "₹2,651.97m")]
    # one citation per table cell: two swapped ids are both caught
    swapped = "| FY26 | 11,716.54 [C4804] | 2,651.97 [C4803] |"
    assert flagged(swapped, REV, PAT) == [(4804, "value", "11,716.54"), (4803, "value", "2,651.97")]
    # a stated limit: inside one sentence each claim finds its value, so two swapped ids pass
    assert flagged("Revenue was ₹11,716.54m [C4804] and PAT ₹2,651.97m [C4803].", REV, PAT) == []


# ------------------------------------------------------------------ what is not judged (the issue's item 2)
def test_claims_cited_as_inputs_of_a_computed_figure_are_not_judged():
    cap = claim(4847, "272", "INR per share", metric="price_band_upper", statement="The upper band is ₹272.")
    vwap = claim(5003, "266.25", "INR per share", metric="anchor_vwap", statement="Anchor VWAP was ₹266.25.")
    for line in ("The cap is about +2.16% above it (fincalc from [C4847][C5003]).",
                 "A premium of 2.16% (fincalc; inputs [C4847][C5003]).",
                 "It is 2.16 percentage points higher (fincalc, from inputs [C4847], [C5003]).",
                 "| Premium to VWAP | 2.16%, i.e. ₹5.75 per share | fincalc; inputs [C4847][C5003] |",
                 "| Premium | 2.16% | [C4801]; via fincalc from [C4847][C5003] |"):  # fmt: skip
        rep = run(line, cap, vwap)
        assert rep.findings == [] and rep.outcomes["input"] == rep.judged == 2, line
    # without the word that marks them as inputs the same citations are judged
    assert (
        flagged("The cap is about +2.16% above it [C4847][C5003].", cap, vwap) == []
    )  # a % is not a ₹ price
    assert flagged("The cap is ₹277.20 [C4847].", cap) == [(4847, "value", "₹277.20")]


def test_a_figure_computed_from_a_printed_claim_value_passes_on_that_value():
    close = claim(5054, "1322", "INR per share", metric="last_close", statement="The last close was ₹1,322.")
    rep = run("The cap is a 25.9 % premium to ₹1,322 [C5054].", close)
    assert rep.findings == [] and rep.outcomes["value"] == 1


def test_claims_without_a_value_and_scopes_without_a_figure_are_not_judged():
    board = claim(
        4900, None, claim_type="factual", statement="Three independent directors; one joined 31-Aug-2026."
    )
    cfo = claim(
        560, "-266.44", "INR million", metric="cfo", statement="Net cash used in operations ₹(266.44)m."
    )
    rep = run("Revenue grew 42.03% and PAT only 0.45%. The board is thin [C4900].", board)
    assert rep.judged == 0 and rep.findings == []
    rep = run("Operating cash flow was negative in FY25, FY26 and Q1 FY27 [C560].", cfo)
    assert rep.outcomes["no_number"] == 1 and rep.findings == []
    # the previous sentence's figure is not this citation's
    rep = run("Revenue grew 42.03%. Operating cash flow stayed negative [C560].", cfo)
    assert rep.outcomes["no_number"] == 1 and rep.findings == []
    assert run("Unknown claims are the gate's business [C999].", cfo).judged == 0


# ------------------------------------------------------------------ layouts of live reports
LIVE = [
    claim(4798, "807.06", "INR million", metric="pat", period="FY2026", statement="PAT was ₹807.06 million."),
    claim(4801, "556.76", "INR million", metric="pat", period="FY2025", statement="PAT was ₹556.76 million."),
    claim(4802, "412.30", "INR million", metric="pat", period="FY2024", statement="PAT was ₹412.30 million."),
    claim(4791, "40.35", "x", metric="pe_basic", statement="P/E on basic EPS is 40.35x."),
    claim(4792, "41.25", "x", metric="pe_diluted", statement="P/E on diluted EPS is 41.25x."),
    claim(4688, "6847.20", "INR million", metric="issue_size", statement="Total issue ₹6,847.20 million."),
    claim(4687, "3976.54", "INR million", metric="fresh_issue", statement="Fresh issue ₹3,976.54 million."),
    claim(4847, "272", "INR per share", metric="price_band_upper", statement="The upper band is ₹272."),
    claim(5003, "266.25", "INR per share", metric="anchor_vwap", statement="Anchor VWAP was ₹266.25."),
    claim(4811, "1.8", "x", metric="retail_cap", statement="The retail cap is 1.80x."),
    claim(5017, "65.42", "%", metric="anchor_mf_share", statement="Domestic mutual funds took 65.42% of the book."),
    claim(4949, "5839.80", "INR million", metric="anchor_amount", statement="The anchor book raised ₹5,839.80m."),
    claim(5022, "194805", "INR", metric="hni_lot_value", statement="The small-HNI lot costs ₹1,94,805."),
    claim(4818, "98.80", "INR million", metric="esop_charge", statement="ESOP charge of ₹98.80 million in FY2026."),
    claim(5167, "0.0793", "x", metric="qib_subscription", period="2026-09-29 16:09 IST",
          statement="QIB was subscribed 0.0793x at 16:09 IST on 29-Sep-2026, all from foreign investors."),
    claim(4984, "36.92", "x", metric="peer_pe_median", statement="Peer P/E ranges 29.84x to 43.99x, median 36.92x."),
    claim(4990, None, claim_type="factual", statement="The registrar is KFin Technologies."),
]  # fmt: skip
LIVE_REPORT = """\
| PAT (profit after tax) | 807.06 [C4798] | 556.76 [C4801] | 412.30 [C4802] |
| P/E on FY26 basic / diluted EPS | 40.35x [C4791] / 41.25x [C4792] |

Only ₹3,976.54m of ₹6,847.20m [C4688][C4687] is new money, about +2.16% (fincalc from [C4847][C5003]).
For retail the cap is 1.80x [C4811], and domestic MFs took 65.42% [C5017][C4949].
One lot costs ₹1,94,805 [C5022]. There is a ₹98.80m FY26 charge for ESOPs [C4818].
QIB stood at 0.0793x, all from foreign investors, at 16:09 IST [C5167].
Peers trade at 29.84x–43.99x FY26 earnings [C4984]. Allotment is on 30-Sep-2026 with 55 lots left [C4990].
"""


def test_the_layouts_of_a_live_report_pass():
    rep = run(LIVE_REPORT, *LIVE)
    assert rep.findings == [] and rep.two_values == []
    # 4984 holds the median and prints the two ends of its range; 4949 stands beside another claim's percentage
    assert rep.outcomes == {"value": 12, "statement": 1, "input": 2, "no_number": 0, "other_figures": 1,
                            "mismatch": 0}  # fmt: skip
    assert [o for i, _, o in rep.cites if i in (4984, 4949)] == ["other_figures", "statement"]


@pytest.mark.parametrize("right,wrong,expected", [
    ("556.76 [C4801]", "565.76 [C4801]", (4801, "value", "565.76")),  # one cell of several in a row
    ("41.25x [C4792]", "42.15x [C4792]", (4792, "value", "42.15x")),  # the second of two in one cell
    ("₹6,847.20m [C4688]", "₹6,874.20m [C4688]", (4688, "value", "₹6,874.20m")),  # reversed pair: the other is right
    ("1.80x [C4811]", "1.08x [C4811]", (4811, "value", "1.08x")),
    ("₹1,94,805", "₹1,94,850", (5022, "value", "₹1,94,850")),
    ("₹98.80m", "₹98.80 crore", (4818, "scale", "₹98.80 crore")),
    ("0.0793x", "0.0739x", (5167, "value", "0.0739x")),  # the time after it is never the value
    ("43.99x", "49.33x", (4984, "value", "49.33x")),  # one end of a range that is in the claim's statement
])  # fmt: skip
def test_one_planted_error_in_a_live_layout_flags_exactly_that_claim(right, wrong, expected):
    assert right in LIVE_REPORT
    assert flagged(LIVE_REPORT.replace(right, wrong), *LIVE) == [expected]


def test_a_source_column_covers_the_figures_of_its_row():
    q1 = claim(
        571, "4891.56", "INR million", metric="revenue", period="Q1 FY2027", statement="₹4,891.56 million."
    )
    fy26 = claim(
        573, "11716.54", "INR million", metric="revenue", period="FY2026", statement="₹11,716.54 million."
    )
    fy25 = claim(
        575, "8249.58", "INR million", metric="revenue", period="FY2025", statement="₹8,249.58 million."
    )
    row = "| Revenue from operations | 4,891.56 | 11,716.54 | 8,249.58 | [C571][C573][C575] |"
    rep = run(
        "| Metric | Q1 FY27 | FY26 | FY25 | Source [C571] |\n|---|---|---|---|---|\n" + row, q1, fy26, fy25
    )
    assert rep.findings == [] and rep.outcomes["value"] == 3 and rep.outcomes["no_number"] == 1  # the header
    assert flagged(row.replace("8,249.58", "8,294.58"), q1, fy26, fy25) == [(575, "value", "8,294.58")]
    assert (
        flagged(row.replace("4,891.56", "(4,891.56)"), q1, fy26, fy25) == []
    )  # an outflow bracket, a bare claim


def test_a_citation_after_words_in_a_cell_without_figures_supports_the_words():
    """Live run 5's scenario table: the row's prices and P&L are the author's assumptions, the last cell argues."""
    qib = claim(920, "0.057377", "x", metric="qib_subscription", statement="QIB subscription was 0.0574x.")
    row = "| Listing | Bear | 245–272 | −9.93% to 0% | −1,485 to 0 | Plausible | QIB stays thin [C920] |"
    rep = run(row, qib)
    assert rep.findings == [] and rep.outcomes["no_number"] == 1
    assert flagged("| QIB (ex-anchor) | 4,279,069 | 245,520 | 0.075x | [C920] |", qib) == [
        (920, "value", "0.075x")
    ]


def test_a_claim_holding_several_periods_supports_its_own_figures_and_no_other():
    conc = claim(594, "76.52", "%", metric="customer_concentration_top10",
                 statement="Largest customer 38.54% (Q1 FY2027), 25.75% (FY2026), 29.29% (FY2025); top 10: 84.00% "
                           "(Q1 FY2027), 76.52% (FY2026), 74.75% (FY2025).")  # fmt: skip
    assert run("| FY26 | 25.75% | 76.52% | [C594] |", conc).outcomes["value"] == 1
    rep = run("| FY25 | 29.29% | 74.75% | [C594] |", conc)
    assert rep.findings == [] and rep.outcomes["statement"] == 1
    assert flagged("| FY25 | 29.92% | 74.75% | [C594] |", conc) == [(594, "value", "29.92%")]


def test_a_citation_after_the_full_stop_belongs_to_the_sentence_before_it():
    eps = claim(31, "5.27", "INR per share", metric="eps", statement="Basic EPS was ₹5.27 in FY2026.")
    assert flagged("Basic EPS was ₹5.72. [C31] The trend is flat.", eps) == [(31, "value", "₹5.72")]
    assert (
        flagged("Rs. 5.27 a share was earned, i.e. about ₹5 [C31].", eps) == []
    )  # "Rs." and "i.e." end nothing
    assert flagged("- **EPS.** ₹5.27 in FY26 [C31]. Revenue was ₹11,716.54m.", eps) == []


# ------------------------------------------------------------------ figures the report does not state itself
def test_a_bound_the_author_sets_is_not_held_against_the_claim():
    qib = claim(920, "0.057377", "x", metric="qib_subscription", statement="QIB subscription was 0.0574x.")
    for line in ("Apply only if QIB (ex-anchor) is at least 1.0x [C920].", "Skip if QIB stays below 1.0x [C920].",
                 "Wait until the book exceeds 1.0x [C920]."):  # fmt: skip
        assert flagged(line, qib) == [], line
    assert flagged("QIB (ex-anchor) was 1.0x [C920].", qib) == [(920, "value", "1.0x")]
    assert flagged("QIB (ex-anchor) was 0.06x at the close [C920].", qib) == []


def test_a_quoted_figure_is_not_held_against_the_claim():
    suit = claim(809, "20", "INR million", metric="damages_claimed",
                 statement="The counter-suit seeks damages of ₹20.00 million (₹2 crore).")  # fmt: skip
    assert flagged('Any "₹20 crore" figure in secondary coverage is wrong [C809].', suit) == []
    assert flagged("The counter-suit seeks ₹20 crore [C809].", suit) == [(809, "scale", "₹20 crore")]
    assert flagged("The counter-suit seeks ₹20.00m (₹2 crore) in damages [C809].", suit) == []


def test_a_labelled_computation_is_nobodys_ledger_value_here_or_repeated_later():
    face = claim(
        2215, "1000", "INR per bond", metric="redemption_amount", statement="Redeems at ₹1,000 per bond."
    )
    price = claim(2144, "1076", "INR", metric="last_price", statement="The last traded price was ₹1,076.")
    later = "The ₹76 premium is lost at redemption [C2215]."
    assert flagged(later, face, price) == [(2215, "value", "₹76")]  # alone: ₹76 is nothing the ledger holds
    row = "| Premium to face value | 7.6%, i.e. ₹76 per bond | fincalc; inputs [C2215][C2144] |"
    assert flagged(row + "\n\n" + later, face, price) == []
    assert flagged("The premium is ₹76 (fincalc), lost at redemption.\n\n" + later, face, price) == []
    rep = run("Net debt after the repayment is about ₹1,010.15m (fincalc) [C2144].", price)
    assert rep.findings == [] and rep.outcomes["other_figures"] == 1
    # a labelled cell does not excuse the direct figures of its row
    ebitda = claim(
        737, "963.97", "INR million", metric="ebitda", statement="EBITDA was ₹963.97 million in FY2026."
    )
    mixed = "| EBITDA | 548.87 (fincalc) | 936.97 | [C737]; Q1 via fincalc from [C2144] |"
    assert flagged(mixed, ebitda, price) == [(737, "value", "936.97")]


def test_a_bare_integer_is_held_against_a_count_only_with_its_unit_word():
    lot = claim(549, "55", "shares", metric="lot_size", statement="The bid lot is 55 equity shares.")
    assert flagged("Bid one lot of 54 shares [C549].", lot) == [(549, "value", "54")]
    assert flagged("Bid one lot of 55 shares [C549].", lot) == []
    assert flagged("See note 8 on lots [C549].", lot) == []
    nav = claim(1952, "138.78", "INR per unit", metric="nav", statement="NAV was 138.78 on 2026-09-28.")
    assert flagged("Direct Plan, Growth Option, AMFI code 120505 [C1952].", nav) == []  # live run 11
    year = claim(2145, "2029", "year", metric="maturity_year", statement="Matures on 2029-03-13.")
    assert run("Maturing 13 Mar 2029 [C2145].", year).outcomes["value"] == 1


# ------------------------------------------------------------------ one metric and period, two values
def two(report, *claims):
    return [(t.a, t.b, t.a_line, t.b_line) for t in run(report, *claims).two_values]


def test_one_metric_and_period_printed_with_two_values_is_reported_separately():
    a = claim(
        635, "535.61", "INR million", metric="pat", period="FY2026", statement="PAT was ₹535.61 million."
    )
    b = claim(
        900, "553.61", "INR million", metric="PAT", period="FY 2026", statement="PAT of ₹553.61 million."
    )
    report = (
        "## Financials\n\nFY26 PAT was ₹535.61m [C635].\n\n## Valuation\n\nOn FY26 PAT of ₹553.61m [C900].\n"
    )
    rep = run(report, a, b)
    assert rep.findings == []  # each figure is its own claim's: this is a different finding
    (t,) = rep.two_values
    assert (t.a, t.b, t.a_line, t.b_line) == (635, 900, 3, 7)
    assert t.message() == ("pat (FY2026) is printed with two values: 535.61 INR million [C635] on line 3 and "
                           "553.61 INR million [C900] on line 7")  # fmt: skip
    close = claim(
        901, "536.61", "INR million", metric="pat", period="FY2026", statement="PAT of ₹536.61 million."
    )
    assert (
        two(report.replace("553.61m [C900]", "536.61m [C901]"), a, close) == []
    )  # 0.19 % apart: within 0.5 %
    crore = claim(
        902, "53.561", "INR crore", metric="pat", period="FY2026", statement="PAT of ₹53.561 crore."
    )
    assert two(report.replace("₹553.61m [C900]", "₹53.561 crore [C902]"), a, crore) == []  # the same amount


def test_claims_set_side_by_side_or_in_two_currencies_are_not_two_values():
    """The ledger's metric and period do not name the entity: live runs hold one "peer_pe" claim per peer."""
    rr = claim(768, "54.85", "x", metric="peer_pe", period="FY2026", statement="RR Kabel P/E 54.85x.")
    pc = claim(770, "46.69", "x", metric="peer_pe", period="FY2026", statement="Polycab P/E 46.69x.")
    table = "| RR Kabel | 54.85x [C768] |\n| Polycab | 46.69x [C770] |\n"
    assert two(table + "\nRR Kabel trades at 54.85x [C768].\n", rr, pc) == []
    assert two("P/E is 54.85x for RR Kabel [C768] and 46.69x for Polycab [C770].", rr, pc) == []
    # never side by side: reported (a warning; the reader decides whether they are two peers)
    assert two("RR Kabel trades at 54.85x [C768].\n\nPolycab trades at 46.69x [C770].\n", rr, pc) == [
        (768, 770, 1, 3)
    ]
    inr = claim(1404, "178650", "INR crore", metric="revenue", period="FY2026", statement="₹1,78,650 crore.")
    usd = claim(
        1490, "20158", "USD million", metric="revenue", period="FY2026", statement="US$20,158 million."
    )
    assert two("Revenue ₹1,78,650 cr [C1404].\n\nIn dollars, US$20,158 mn [C1490].\n", inr, usd) == []
    pct = claim(41, "15", "%", metric="roe", period="FY2026", statement="ROE 15%.")
    frac = claim(42, "0.18", "fraction", metric="roe", period="FY2026", statement="ROE 0.18.")
    assert two("ROE was 15% [C41].\n\nROE of 18% [C42].\n", pct, frac) == [(41, 42, 1, 3)]


# ------------------------------------------------------------------ in the publish gate
@pytest.fixture
def ledger(env, tmp_path):
    """A run with three verified exchange facts: two high-importance, one normal."""
    from finresearch.db import session_scope
    from finresearch.db.models import Claim, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        co = get_or_create_company(s, "printed-" + tmp_path.name[-8:], "Printed Co")
        run_ = ResearchRun(company_id=co.id, kind="test", manifest={})
        s.add(run_)
        s.flush()
        ids = {}
        for key, metric, value, unit, importance in (("pat", "pat", "2651.97", "INR million", "high"),
                                                     ("band", "price_band_upper", "272", "INR per share", "high"),
                                                     ("lot", "lot_size", "55", "shares", "normal")):  # fmt: skip
            c = Claim(run_id=run_.id, stream="facts", statement=f"{metric} is {value} {unit}", claim_type="numeric",
                      metric=metric, value=Decimal(value), unit=unit, period="FY2026", importance=importance,
                      status="verified", checks={"source": "nse_issue_info"})  # fmt: skip
            s.add(c)
            s.flush()
            ids[key] = c.id
        return {"run": run_.id, **ids}


def gate(led, report):
    from finresearch.db import session_scope
    from finresearch.verify.gate import check_report

    with session_scope() as s:
        return check_report(s, led["run"], report)


def test_the_publish_gate_warns_about_a_printed_mismatch_and_says_what_to_fix(ledger):
    from finresearch.verify import gate as gate_mod

    assert gate_mod.PRINTED_VALUE_BLOCKS is False  # the measured default: see the policy note in verify.gate
    pat, band, lot = ledger["pat"], ledger["band"], ledger["lot"]
    clean = f"PAT was ₹2,651.97m [C{pat}] at the ₹272 cap [C{band}], in lots of 55 shares [C{lot}].\n"
    g = gate(ledger, clean)
    assert g.ok and g.warnings == [] and g.blocking == []
    g = gate(ledger, clean.replace("2,651.97m", "2,561.97m").replace("55 shares", "54 shares"))
    assert g.ok and g.blocking == []  # a warning at every importance while the switch is off
    assert g.warnings == [
        f"[C{pat}] holds 2,651.97 INR million (pat, FY2026) but the report prints ₹2,561.97m beside it, line 1: "
        f'"PAT was ₹2,561.97m [C{pat}]" — print the claim\'s value, cite the claim that holds this figure, or label '
        "it as a fincalc result with its inputs",
        f"[C{lot}] holds 55 shares (lot_size, FY2026) but the report prints 54 beside it, line 1: "
        f'"PAT was ₹2,561.97m [C{pat}] at the ₹272 cap [C{band}], in lots of 54 shares [C{lot}]" — print the '
        "claim's value, cite the claim that holds this figure, or label it as a fincalc result with its inputs",
    ]
    assert f"[C{pat}] holds 2,651.97 INR million" in g.revision_request()


def test_printed_findings_come_before_the_other_warnings(ledger):
    """`revision_request` sends the first 30 warnings and the run manifest keeps 50: a report with many caveat
    warnings must not push the one that says which figure is wrong out of sight."""
    pat = ledger["pat"]
    lines = "".join(f"Margin was {i}.5% in segment {i}.\n" for i in range(40))  # an "uncited figures" warning
    g = gate(ledger, lines + f"PAT was ₹2,561.97m [C{pat}].\n")
    assert len(g.warnings) == 2 and g.warnings[0].startswith(f"[C{pat}] holds 2,651.97 INR million")
    assert "line 41" in g.warnings[0] and "without a [C<id>] citation" in g.warnings[1]


def test_with_the_switch_on_only_high_importance_mismatches_block(ledger, monkeypatch):
    from finresearch.verify import gate as gate_mod

    monkeypatch.setattr(gate_mod, "PRINTED_VALUE_BLOCKS", True)
    pat, band, lot = ledger["pat"], ledger["band"], ledger["lot"]
    g = gate(
        ledger,
        f"PAT was ₹2,651.97 crore [C{pat}] at the ₹272 cap [C{band}], in lots of 54 shares [C{lot}].\n",
    )
    assert not g.ok and len(g.blocking) == 1 and len(g.warnings) == 1
    assert g.blocking[0].startswith(f"[C{pat}] holds 2,651.97 INR million (pat, FY2026) but the report prints "
                                    "₹2,651.97 crore beside it (the same digits in another scale), line 1")  # fmt: skip
    assert g.warnings[0].startswith(f"[C{lot}] holds 55 shares")  # normal importance: still a warning
    assert g.revision_request().startswith("REVISION REQUIRED") and f"[C{pat}] holds" in g.revision_request()
    assert gate(ledger, f"PAT was ₹2,651.97m [C{pat}] at the ₹272 cap [C{band}].\n").ok


def test_two_values_reach_the_gate_as_a_warning_even_with_the_switch_on(ledger, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.verify import gate as gate_mod

    monkeypatch.setattr(gate_mod, "PRINTED_VALUE_BLOCKS", True)
    with session_scope() as s:
        other = Claim(run_id=ledger["run"], stream="valuation", statement="PAT of 2,516.97 million", metric="PAT",
                      claim_type="numeric", value=Decimal("2516.97"), unit="INR million", period="FY 2026",
                      importance="high", status="verified", checks={"source": "nse_issue_info"})  # fmt: skip
        s.add(other)
        s.flush()
        oid = other.id
    g = gate(
        ledger, f"PAT was ₹2,651.97m [C{ledger['pat']}].\n\nOn PAT of ₹2,516.97m [C{oid}] the P/E is high.\n"
    )
    assert g.ok and g.warnings == [
        f"pat (FY2026) is printed with two values: 2,651.97 INR million [C{ledger['pat']}] on line 1 and 2,516.97 "
        f"INR million [C{oid}] on line 3 — print one value, or say why the two differ"]  # fmt: skip


# ------------------------------------------------------------------ the exported live runs: the measurement of #502
# fixture -> (cited claims that hold a value, the outcome of each of their citations, findings (claim, kind, printed
# figure, line)). Every finding was read by hand against the fixture's ledger (see the test below).
MEASURED = {
    "bond-ine027e07998-run12": (33, {"value": 48, "statement": 7, "input": 40, "no_number": 12, "other_figures": 5,
                                     "mismatch": 0}, []),
    "infosys-run9": (148, {"value": 185, "statement": 55, "input": 0, "no_number": 13, "other_figures": 9,
                           "mismatch": 0}, []),
    "mf-120505-run11": (81, {"value": 128, "statement": 13, "input": 4, "no_number": 6, "other_figures": 6,
                             "mismatch": 0}, []),
    "orient-cables-run5": (184, {"value": 211, "statement": 26, "input": 25, "no_number": 37, "other_figures": 16,
                                 "mismatch": 2}, [(656, "value", "11,764,705", 31), (744, "value", "41.13%", 143)]),
}  # fmt: skip


def exported(name):
    data = json.loads((EVAL / f"{name}.json").read_text())
    return data["synthesis"]["report_markdown"], {c["id"]: SimpleNamespace(**c) for c in data["claims"]}


@pytest.mark.parametrize("name", sorted(MEASURED))
def test_the_exported_live_runs_are_measured(name):
    report, claims = exported(name)
    rep = check_printed(report, claims)
    cited, outcomes, findings = MEASURED[name]
    assert len({i for i, _, _ in rep.cites}) == cited
    assert rep.outcomes == outcomes and rep.judged == sum(outcomes.values())
    assert [(f.claim_id, f.kind, f.printed, f.line) for f in rep.findings] == findings
    assert rep.two_values == []


def test_the_two_findings_of_live_run_5_are_real_wrong_citations():
    """Both are the right figure beside the wrong claim id, on normal-importance claims; the claim that does hold
    each printed figure exists in the ledger and is not the one cited."""
    report, claims = exported("orient-cables-run5")
    lines = report.splitlines()
    # line 31 prints the fresh-issue share count at the cap (C654) but cites the floor-price count (C656)
    assert "Fresh issue of 11,764,705 shares at the cap [C656]" in lines[30]
    assert (claims[656].value, claims[656].period) == ("12403100.000000", "IPO 2026, floor price ₹258")
    assert (claims[654].value, claims[654].period) == ("11764705.000000", "IPO 2026, cap price ₹272")
    # line 143 prints FY24 and FY26 ROCE but cites the FY25 claim (C744) for the first; FY24's is C745
    assert lines[142] == "| ROCE (RHP) | 41.13% (FY24) → 23.82% (FY26) | [C744][C742] |"
    assert [(claims[i].value, claims[i].period) for i in (744, 742, 745)] == [
        ("36.460000", "FY2025"), ("23.820000", "FY2026"), ("41.130000", "FY2024")]  # fmt: skip
    assert {claims[i].importance for i in (656, 744)} == {"normal"}


def test_a_replayed_live_run_warns_and_never_blocks_on_its_two_findings(env, tmp_path, monkeypatch):
    """Through the gate, with remapped ids: two warnings, first in the list; with the switch on they still only
    warn (normal importance), so the replay pins of tests/test_replay.py hold under either setting."""
    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.evals.replay import import_run, remap_synthesis
    from finresearch.verify import gate as gate_mod

    data = json.loads((EVAL / "orient-cables-run5.json").read_text())
    with session_scope() as s:
        run_id = import_run(s, data, slug_suffix="-" + tmp_path.name[-8:])
        new = sorted(c.id for c in s.query(Claim).filter_by(run_id=run_id))
        ids = dict(zip([c["id"] for c in data["claims"]], new, strict=True))
        report = remap_synthesis(data["synthesis"], ids)["report_markdown"]
        for switch in (False, True):
            monkeypatch.setattr(gate_mod, "PRINTED_VALUE_BLOCKS", switch)
            g = gate_mod.check_report(s, run_id, report)
            assert [w.split(" holds ")[0] for w in g.warnings[:2]] == [f"[C{ids[656]}]", f"[C{ids[744]}]"]
            assert not any("but the report prints" in b for b in g.blocking)
            assert sum("but the report prints" in w for w in g.warnings) == 2


# ------------------------------------------------------------------ recall: mismatches planted in real report text
def transposed(number: str, *, last: bool) -> str | None:
    """The number with its first (or last) two differing neighbouring digits swapped."""
    at = [i for i in range(len(number) - 1)
          if number[i].isdigit() and number[i + 1].isdigit() and number[i] != number[i + 1]]  # fmt: skip
    if not at:
        return None
    i = at[-1] if last else at[0]
    return number[:i] + number[i + 1] + number[i] + number[i + 2 :]


def plant(name: str, *, last: bool, step: int = 1) -> tuple[int, int]:
    """(caught, planted): corrupt, one at a time, the figure of every `step`-th citation whose claim value is
    printed exactly once on its line, and count the plants that raise a finding for that claim on that line."""
    report, claims = exported(name)
    claims = {i: c for i, c in claims.items() if f"[C{i}]" in report}
    lines = report.split("\n")
    sites = sorted({(i, no) for i, no, outcome in check_printed(report, claims).cites if outcome == "value"})
    caught = planted = 0
    for cid, no in sites[::step]:
        hits = [f for f in figures(lines[no - 1]) if matches(f, Decimal(claims[cid].value), claims[cid].unit)]
        number = (
            re.search(r"\d[\d,]*(?:\.\d+)?", hits[0].text).group(0).rstrip(",") if len(hits) == 1 else None
        )
        wrong = transposed(number, last=last) if number else None
        if wrong is None:
            continue
        at = hits[0].start + lines[no - 1][hits[0].start :].index(number)
        line = lines[no - 1][:at] + wrong + lines[no - 1][at + len(number) :]
        rep = check_printed("\n".join([*lines[: no - 1], line, *lines[no:]]), claims)
        planted += 1
        caught += any(f.claim_id == cid and f.line == no for f in rep.findings)
    return caught, planted


@pytest.mark.parametrize("name,caught,planted", [("bond-ine027e07998-run12", 10, 10), ("infosys-run9", 24, 27),
                                                 ("mf-120505-run11", 24, 25), ("orient-cables-run5", 37, 39)])  # fmt: skip
def test_transposed_digits_planted_in_the_live_reports_are_caught(name, caught, planted):
    """A sample (every 5th citation) of the measurement in the PR of #502, where `plant(name, last=..., step=1)`
    over the four runs caught 467 of 494 plants of the first pair of digits and 463 of 494 of the last. The misses
    are figures the rule does not judge on purpose (a bound, a labelled computation, a bare integer without its
    unit word, a date). Making the rule more lenient must show up here."""
    assert plant(name, last=False, step=5) == (caught, planted)
    assert plant(name, last=True, step=5) == (caught, planted)
