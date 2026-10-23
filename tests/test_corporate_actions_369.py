"""Corporate-action adjustment of F&O and equity histories (#369, roadmap P1.7).

Synthetic prices and lots only. Expected values are computed by hand from the issue's formulas:
F&O factor f = L_t / L_t-1, adjusted return r_t = ln(F_t x f / F_t-1)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from finresearch.fincalc import fno_history as fh

E = date(2026, 10, 27)
D = [date(2026, 9, 1) + timedelta(days=i) for i in range(4)]


def _futs(px):
    return [(d, E, p) for d, p in zip(D, px, strict=False)]


def _lots(seq):
    return {(d, E): lot for d, lot in zip(D, seq, strict=False)}


def test_bonus_lot_change_is_adjusted_not_dropped():
    # 1:1 bonus: NSE halves the open future (1010 -> 500) and doubles the lot (500 -> 1000): f = 2,
    # adjusted return ln(500 x 2 / 1010) = ln(1000 / 1010), a -1 % market move kept in RV
    adjusted: list = []
    unverified: list = []
    rets = fh.front_future_returns(_futs([1000.0, 1010.0, 500.0]), lots=_lots([500, 500, 1000]),
                                   unverified=unverified, adjusted=adjusted)  # fmt: skip
    assert [d for d, _ in rets] == [D[1], D[2]]
    assert rets[1][1] == pytest.approx(math.log(1000 / 1010))
    assert unverified == []
    assert adjusted == [(D[2], E, pytest.approx(2.0), pytest.approx(math.log(500 / 1010)))]


def test_lot_cut_on_consolidation_is_adjusted():
    # 1-for-5 consolidation: price x5 (200 -> 1000), lot / 5 (2500 -> 500): f = 0.2, r = ln(1000 x 0.2 / 200) = 0
    rets = fh.front_future_returns(_futs([200.0, 1000.0]), lots=_lots([2500, 500]))
    assert rets == [(D[1], pytest.approx(0.0))]


def test_lot_revision_without_price_adjustment_keeps_raw_return():
    # NSE's periodic lot revision (no corporate action): lot 500 -> 750 but the price moves +1 %. Applying
    # f = 1.5 would invent a ln(1.5) jump; the raw return is the smaller, verified one
    adjusted: list = []
    rets = fh.front_future_returns(_futs([1000.0, 1010.0]), lots=_lots([500, 750]), adjusted=adjusted)
    assert rets == [(D[1], pytest.approx(math.log(1.01)))]
    assert adjusted == []


def test_lot_change_whose_factor_does_not_explain_the_jump_is_unverified():
    # lot doubles but the price fell 80 %: adjusted ln(0.2 x 2) = -0.92, raw -1.61, neither within 0.25
    unverified: list = []
    rets = fh.front_future_returns(_futs([1000.0, 200.0]), lots=_lots([500, 1000]), unverified=unverified)
    assert rets == []
    assert unverified == [(D[1], E, pytest.approx(math.log(0.2)))]


@pytest.mark.parametrize("lots", [None, _lots([500, 500, 500])])
def test_unexplained_jump_is_unverified_and_excluded(lots):
    # a -50 % within-contract day with no lot change (or no lot field, pre-UDiFF days) is not a verified market
    # move: excluded from RV and reported, never silently kept
    unverified: list = []
    rets = fh.front_future_returns(_futs([1000.0, 1010.0, 505.0]), lots=lots, unverified=unverified)
    assert [d for d, _ in rets] == [D[1]]
    assert unverified == [(D[2], E, pytest.approx(math.log(0.5)))]


def test_ordinary_moves_pass_through_unchanged():
    rets = fh.front_future_returns(_futs([1000.0, 1100.0, 990.0]))
    assert [r for _, r in rets] == [pytest.approx(math.log(1.1)), pytest.approx(math.log(0.9))]


async def test_closes_raise_when_corporate_actions_cannot_be_fetched(monkeypatch):
    # before #369 a failed corporate-actions fetch returned unadjusted closes, so a 1:1 bonus read as a -50 % day in
    # the F&O risk gate's realised vol; now the closes are unknown (NseError), never silently unadjusted
    from decimal import Decimal

    from finresearch.adapters import nse_equity
    from finresearch.adapters.nse import NseError
    from finresearch.adapters.nse_equity import PriceBar
    from finresearch.adapters.nse_fno import NseFno

    days = [date(2026, 5, 4) + timedelta(days=i) for i in range(5)]

    class FakeEq:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def history(self, sym, lo, hi):
            return [PriceBar(day=d, open=None, high=None, low=None, close=Decimal(100), prev_close=None, vwap=None,
                             volume=None, value_inr=None, trades=None) for d in days if lo <= d <= hi]  # fmt: skip

        async def corporate_actions(self, sym):
            raise NseError("HTTP 403 for corporate actions")

    monkeypatch.setattr(nse_equity, "NseEquity", FakeEq)
    with pytest.raises(NseError, match="corporate actions"):
        await NseFno(client=object()).closes("EXAMPLE", days[0], days[-1])  # type: ignore[arg-type]


# --------------------------------------------------------------------------- equity factor maths (fincalc.corporate_actions)
def test_factor_formulas():
    from decimal import Decimal as Dc

    from finresearch.fincalc import corporate_actions as ca

    assert ca.bonus_factor(1, 1) == 2  # 1 new for every 1 held
    assert ca.bonus_factor(3, 2) == Dc("2.5")  # (3 + 2) / 2
    assert ca.face_value_factor(Dc(10), Dc(2)) == 5  # split Rs 10 -> Rs 2
    assert ca.face_value_factor(Dc(1), Dc(10)) == Dc("0.1")  # consolidation Rs 1 -> Rs 10
    # rights 1 for 4 at Rs 60, cum-rights 100: TERP = (4 x 100 + 1 x 60) / 5 = 92, f = 100 / 92
    assert ca.rights_factor(1, 4, Dc(100), Dc(60)) == Dc(100) / Dc(92)
    # a rights issue priced at or above the market changes nothing
    assert ca.rights_factor(1, 4, Dc(100), Dc(120)) == 1
    # demerger: cum price 500, the demerged unit is worth 125 per share: f = 500 / 375
    assert ca.demerger_factor(Dc(500), Dc(125)) == Dc(500) / Dc(375)


@pytest.mark.parametrize(
    ("call", "args"),
    [("bonus_factor", (0, 1)), ("bonus_factor", (1, 0)), ("face_value_factor", (0, 2)),
     ("rights_factor", (1, 0, 100, 60)), ("demerger_factor", (100, 100)), ("demerger_factor", (100, -1))],
)  # fmt: skip
def test_factor_formulas_refuse_bad_terms(call, args):
    from decimal import Decimal as Dc

    from finresearch.fincalc import corporate_actions as ca

    with pytest.raises(ValueError):
        getattr(ca, call)(*(Dc(a) for a in args))


def test_cumulative_factor_back_adjusts_before_each_ex_date():
    from decimal import Decimal as Dc

    from finresearch.fincalc import corporate_actions as ca

    days = [date(2025, 1, i) for i in (1, 2, 3, 6, 7)]
    px = [Dc(1000), Dc(1000), Dc(500), Dc(500), Dc(100)]
    events = [(days[2], Dc(2)), (days[4], Dc(5))]  # a 1:1 bonus ex 3-Jan, a 10 -> 2 split ex 7-Jan
    # CF_t = product of the factors whose ex-date is after t: 10, 10, 5, 5, 1
    assert ca.cumulative_factors(days, events) == [Dc(10), Dc(10), Dc(5), Dc(5), Dc(1)]
    assert ca.adjust_prices(days, px, events) == [Dc(100)] * 5


def test_total_return_index_adds_dividends_and_neutralises_factors():
    from decimal import Decimal as Dc

    from finresearch.fincalc import corporate_actions as ca

    days = [date(2025, 1, i) for i in (1, 2, 3, 6)]
    px = [Dc(100), Dc(98), Dc(49), Dc(50)]
    # ex-dividend Rs 2 on 2-Jan (price 100 -> 98: no loss), 1:1 bonus on 3-Jan (98 -> 49: no loss), then +2.04 %
    tr = ca.total_return_index(days, px, factors=[(days[2], Dc(2))], dividends=[(days[1], Dc(2))])
    assert tr == [Dc(1), Dc(1), Dc(1), Dc(50) / Dc(49)]


def test_delisting_return_is_unknown_without_an_exit_price():
    from decimal import Decimal as Dc

    from finresearch.fincalc import corporate_actions as ca

    assert ca.delisting_return(Dc(80), Dc(100)) == Dc("0.25")  # exit offer 100 vs last close 80
    assert ca.delisting_return(Dc(80), Dc(70), dividend=Dc(2)) == Dc(72) / Dc(80) - 1
    assert ca.delisting_return(Dc(80), None) is None  # never a US-style -30 % default


# --------------------------------------------------------------------------- legacy days: factors from the table (slice 2)
def test_lot_less_bonus_day_is_adjusted_from_the_action_table():
    # pre-UDiFF day, no lot field: the stored 1:1 bonus (f = 2) going ex on D[2] explains 1010 -> 500
    adjusted: list = []
    unverified: list = []
    rets = fh.front_future_returns(_futs([1000.0, 1010.0, 500.0]), factors={D[2]: 2.0}, unverified=unverified,
                                   adjusted=adjusted)  # fmt: skip
    assert rets[1] == (D[2], pytest.approx(math.log(1000 / 1010)))
    assert unverified == []
    assert adjusted == [(D[2], E, pytest.approx(2.0), pytest.approx(math.log(500 / 1010)))]


def test_an_ex_date_without_a_session_applies_to_the_next_session():
    # ex-date D[1] has no stored row (a holiday): it lies in (D[0], D[2]], so the D[0] -> D[2] return is adjusted
    futs = [(D[0], E, 1000.0), (D[2], E, 505.0)]
    assert fh.front_future_returns(futs, factors={D[1]: 2.0}) == [
        (D[2], pytest.approx(math.log(1010 / 1000)))
    ]


def test_a_jump_on_another_day_stays_unverified():
    unverified: list = []
    rets = fh.front_future_returns(_futs([1000.0, 500.0, 505.0]), factors={D[2]: 2.0}, unverified=unverified)
    assert [d for d, _ in rets] == [
        D[2]
    ]  # D[2]'s factor is not applied to its own +1 % (it would make it +100 %)
    assert unverified == [(D[1], E, pytest.approx(math.log(0.5)))]


def test_an_unknown_factor_never_adjusts():
    # a demerger went ex (factor unknown, None): the -50 % day is unverified, never treated as f = 1 or adjusted
    unverified: list = []
    assert fh.front_future_returns(_futs([1000.0, 500.0]), factors={D[1]: None}, unverified=unverified) == []
    assert unverified == [(D[1], E, pytest.approx(math.log(0.5)))]


def test_a_table_factor_that_does_not_explain_the_move_is_not_applied():
    unverified: list = []
    assert fh.front_future_returns(_futs([1000.0, 200.0]), factors={D[1]: 2.0}, unverified=unverified) == []
    assert unverified == [(D[1], E, pytest.approx(math.log(0.2)))]


def test_a_lot_change_wins_over_the_table():
    # UDiFF lots say f = 2; a (wrong) table factor of 3 is not consulted when both lots are known
    adjusted: list = []
    fh.front_future_returns(_futs([1000.0, 1010.0, 500.0]), lots=_lots([500, 500, 1000]), factors={D[2]: 3.0},
                            adjusted=adjusted)  # fmt: skip
    assert adjusted[0][2] == pytest.approx(2.0)
