"""Exchange corporate-action terms -> price and share factors (#369, second slice). Pure; synthetic subjects in the
formats NSE (/api/corporates-corporateActions `subject`) and BSE (`Purpose`) print. Expected factors are computed by
hand from the issue's formulas: bonus a:b -> (a+b)/b; split/consolidation FV_old/FV_new; rights TERP."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from finresearch.fincalc.corporate_actions import action_terms


@pytest.mark.parametrize(
    ("subject", "kind", "factor"),
    [
        ("Bonus 1:1", "bonus", D(2)),
        (" Bonus 3:2", "bonus", D("2.5")),  # 3 new for every 2 held: (3+2)/2
        ("Bonus issue 1:1", "bonus", D(2)),  # BSE
        ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", "split", D(5)),
        ("Stock  Split From Rs.10/- to Rs.1/-", "split", D(10)),  # BSE
        ("Face Value Consolidation From Rs 1/- Per Share To Rs 10/- Per Share", "consolidation", D("0.1")),
        ("Bonus 1:1/Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", "bonus+split",
         D(10)),
    ],
)  # fmt: skip
def test_known_terms_give_the_formula_factor(subject, kind, factor):
    t = action_terms(subject)
    assert (t.kind, t.status, t.price_factor, t.share_factor) == (kind, "known", factor, factor)
    assert t.reason is None


def test_rights_at_premium_uses_face_value_plus_premium_and_the_cum_close():
    # 1 new for 5 held at FV 10 + premium 100 = 110; cum close 300: TERP = (5 x 300 + 1 x 110) / 6 = 268.333...,
    # f = 300 / 268.333... = 1800 / 1610 = 1.118012...
    t = action_terms("Rights 1:5 @ Premium Rs 100/-", face_value=D(10), p_cum=D(300))
    assert (t.kind, t.status) == ("rights", "known")
    assert t.price_factor == pytest.approx(D(1800) / D(1610))
    assert t.share_factor == D(1)  # no free shares: the holder pays for the new ones
    assert t.terms == {"n_new": "1", "n_held": "5", "p_sub": "110", "p_cum": "300"}


def test_rights_at_par_and_at_a_stated_price():
    assert action_terms("Rights 1:4 @ Par", face_value=D(2), p_cum=D(12)).price_factor == pytest.approx(
        D(12) / ((4 * D(12) + D(2)) / 5)
    )
    assert action_terms("Rights 3:10 @ Rs 50 Per Share", p_cum=D(80)).price_factor == pytest.approx(
        D(80) / ((10 * D(80) + 3 * D(50)) / 13)
    )


@pytest.mark.parametrize(
    ("subject", "kw", "reason_part"),
    [
        ("Rights 1:5 @ Premium Rs 100/-", {"p_cum": D(300)}, "face value"),  # premium without a face value
        ("Rights 1:5 @ Premium Rs 100/-", {"face_value": D(10)}, "cum-rights close"),
        ("Right Issue of Equity Shares", {"p_cum": D(300)}, "ratio"),  # BSE: no terms in the purpose
        ("Demerger", {}, "demerged"),
        ("Scheme Of Arrangement - Demerger Of Example Retail Business", {}, "demerged"),
        ("Consolidation Of Shares", {}, "face values"),
        ("Scheme Of Amalgamation", {}, "merger"),
        ("Capital Reduction", {}, "capital reduction"),
        ("Change In ISIN", {}, "isin"),
        ("Bonus 1:1/Demerger", {}, "demerged"),  # one unknown part makes the whole ex-date unknown
        ("Bonus 0:1", {}, "bad terms"),
        ("Something New", {}, "unrecognised"),
    ],
)
def test_unknown_terms_keep_the_factor_unknown_with_a_reason(subject, kw, reason_part):
    t = action_terms(subject, **kw)
    assert t.status == "unknown"
    assert t.price_factor is None and t.share_factor is None  # never 1.0
    assert reason_part in t.reason.lower()


@pytest.mark.parametrize(
    ("subject", "kind", "dividend"),
    [
        ("Dividend - Rs 25 Per Share", "dividend", D(25)),
        (
            "Annual General Meeting/Special Dividend - Rs 8 Per Share /Dividend - Rs 20 Per Share",
            "dividend",
            D(28),
        ),
        ("Buy Back", "buyback", None),
        ("Annual General Meeting", "meeting", None),
        ("Interest Payment", "interest", None),
    ],
)
def test_actions_without_a_price_factor_are_none_not_unknown(subject, kind, dividend):
    # a cash dividend, a buyback (a tender offer to some holders; the price series is not rescaled) or a meeting
    # changes neither the share count nor the price basis: factor exactly 1 by definition
    t = action_terms(subject)
    assert (t.kind, t.status, t.price_factor, t.share_factor, t.dividend) == (
        kind,
        "none",
        D(1),
        D(1),
        dividend,
    )


def test_delisting_exit_price_is_terms_not_a_factor():
    t = action_terms("Delisting Of Equity Shares @ Rs 450 Per Share")
    assert (t.kind, t.status, t.price_factor, t.terms["exit_price"]) == ("delisting", "known", None, "450")
    u = action_terms("Voluntary Delisting")
    assert (u.kind, u.status, u.price_factor) == ("delisting", "unknown", None)
    assert "exit price" in u.reason
