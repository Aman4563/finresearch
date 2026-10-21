"""P4.2 (#395): the common-shock and margin-utilisation terms of the one sizing rule, and the Kelly ceiling on real
money. Expected values are hand arithmetic, written out next to each assertion, never the code under test."""

from __future__ import annotations

import math

import pytest

from finresearch.fincalc import tradeplan as tp

M3, M5, M10, P5 = -0.03, -0.05, -0.10, 0.05


def test_the_registered_shock_moves_and_limits_395():
    assert tp.SHOCK_MOVES == (M3, M5, M10, P5)
    assert tp.SHOCK_BUDGET_PCT == 6.0 and tp.MARGIN_UTIL_MAX == 0.5


# --------------------------------------------------------------------------- the common-shock term
BOOK = {M3: -1000.0, M5: -2000.0, M10: -5000.0, P5: 500.0}
ONE = {M3: -300.0, M5: -600.0, M10: -1500.0, P5: 100.0}


def test_shock_lots_is_the_tightest_move_395():
    # limit 20,000: −3 %: (20,000 − 1,000) / 300 = 63.3 → 63; −5 %: 18,000 / 600 = 30; −10 %: 15,000 / 1,500 = 10;
    # +5 %: the new trade gains, no bound → 10
    n, why = tp.shock_lots(BOOK, ONE, 20_000)
    assert n == 10 and "-10%" in why


def test_shock_lots_floors_at_zero_when_one_lot_does_not_fit_395():
    # limit 6,000 at −10 %: (6,000 − 5,000) / 1,500 = 0.67 → 0
    assert tp.shock_lots(BOOK, ONE, 6_000)[0] == 0


def test_a_book_already_beyond_the_shock_budget_takes_nothing_new_even_a_hedge_395():
    book = {**BOOK, P5: -7000.0}  # the open book alone loses ₹7,000 at +5 %, beyond ₹6,000
    n, why = tp.shock_lots(book, {M3: 10.0, M5: 10.0, M10: 10.0, P5: 2000.0}, 6_000)
    assert n == 0 and "already" in why


def test_a_trade_that_loses_in_no_move_is_not_bounded_by_the_shock_term_395():
    n, _ = tp.shock_lots(BOOK, {M3: 1.0, M5: 2.0, M10: 3.0, P5: 0.0}, 6_000)
    assert n == math.inf


@pytest.mark.parametrize(
    "book,one",
    [
        (None, ONE),  # the open book's shock is unknown
        (BOOK, None),
        ({**BOOK, M5: None}, ONE),  # one move unknown
        (BOOK, {**ONE, P5: None}),
        ({M3: 0.0, M5: 0.0, M10: 0.0}, ONE),  # a registered move missing: unknown, never "no loss"
    ],
)
def test_an_unknown_shock_is_never_room_395(book, one):
    n, why = tp.shock_lots(book, one, 1e9)
    assert n is None and "unknown" in why


# --------------------------------------------------------------------------- the margin-utilisation term
def test_margin_utilisation_is_the_larger_of_the_two_ratios_395():
    # (100,000 + 40,000) / (300,000 + 200,000) = 0.28; (140,000) x 0.5 / 300,000 = 0.2333 → 0.28
    assert tp.margin_utilisation(100_000, 40_000, 300_000, 200_000) == pytest.approx(0.28)
    # cash-bound: 140,000 / 500,000 = 0.28; 140,000 x 0.5 / 100,000 = 0.70 → 0.70
    assert tp.margin_utilisation(100_000, 40_000, 100_000, 400_000) == pytest.approx(0.70)


def test_margin_lots_395():
    # capacity 0.5 x min(300,000 + 200,000, 300,000 / 0.5) = 250,000; (250,000 − 100,000) / 40,000 = 3.75 → 3
    assert tp.margin_lots(100_000, 40_000, 300_000, 200_000)[0] == 3
    # cash-bound: 0.5 x min(500,000, 100,000 / 0.5 = 200,000) = 100,000; (100,000 − 50,000) / 40,000 = 1.25 → 1
    assert tp.margin_lots(50_000, 40_000, 100_000, 400_000)[0] == 1
    # already beyond u_max: 0, never negative
    assert tp.margin_lots(300_000, 40_000, 300_000, 200_000)[0] == 0


@pytest.mark.parametrize("args", [(None, 40_000, 3e5, 2e5), (1e5, None, 3e5, 2e5), (1e5, 4e4, None, 2e5),
                                  (1e5, 4e4, 3e5, None)])  # fmt: skip
def test_unknown_margin_or_funds_is_never_room_395(args):
    n, why = tp.margin_lots(*args)
    assert n is None and "unknown" in why


def test_no_cash_means_no_margin_room_395():
    assert tp.margin_lots(0, 40_000, 0, 500_000)[0] == 0


# --------------------------------------------------------------------------- one sizing rule: every term
CAPITAL = 1_000_000.0  # ₹10 lakh: trade budget 2% = ₹20,000, book 8% = ₹80,000, shock 6% = ₹60,000
ML, COSTS = -4940.0, 60.0  # one set risks ₹5,000


def _book(open_risk=0.0, shock=None, funds=None, mtd_real=0.0):
    return tp.OpenBook(capital=CAPITAL, open_risk=open_risk, mtd_paper=0.0, mtd_real=mtd_real, shock=shock,
                       funds=funds)  # fmt: skip


FLAT = {m: 0.0 for m in tp.SHOCK_MOVES}
ONE_SET = {M3: -1500.0, M5: -2500.0, M10: -4800.0, P5: 700.0}


def test_the_shock_term_cuts_the_paper_size_and_says_so_395():
    # trade: floor(20,000 / 5,000) = 4; book: floor(80,000 / 5,000) = 16; shock with the book at −₹45,000 at −10 %:
    # floor((60,000 − 45,000) / 4,800) = 3 → 3 (the binding term)
    book = {**FLAT, M10: -45000.0}
    s = tp.size(ML, COSTS, 20_000.0, proven=False, book=_book(shock=tp.Shock(book, ONE_SET)))
    assert (s.multiple, s.suggested, s.binding) == (4, 3, "shock") and "common shock" in s.reason


def test_an_unknown_shock_sizes_zero_395():
    s = tp.size(ML, COSTS, 20_000.0, proven=True, book=_book(shock=tp.Shock(None, ONE_SET, ("no chain",))))
    assert s.suggested == 0 and s.real_lots == 0 and s.binding == "shock" and "unknown" in s.reason


def test_the_margin_term_caps_real_lots_only_395():
    # margin: 0.5 x min(2,00,000 + 0, 2,00,000 / 0.5) = 1,00,000; (1,00,000 − 40,000) / 25,000 = 2.4 → 2 real lots
    funds = tp.Funds(cash=200_000.0, margin_used=40_000.0, collateral=0.0, im_one=25_000.0)
    s = tp.size(ML, COSTS, 20_000.0, proven=True, book=_book(shock=tp.Shock(FLAT, ONE_SET), funds=funds))
    assert (s.suggested, s.real_lots) == (4, 2) and "margin" in s.real_reason


@pytest.mark.parametrize("funds", [tp.Funds(None, 0.0, 0.0, 25_000.0), tp.Funds(2e5, None, 0.0, 25_000.0),
                                   tp.Funds(2e5, 0.0, None, 25_000.0), tp.Funds(2e5, 0.0, 0.0, None)])  # fmt: skip
def test_unknown_margin_or_funds_blocks_real_money_with_a_reason_395(funds):
    s = tp.size(ML, COSTS, 20_000.0, proven=True, book=_book(shock=tp.Shock(FLAT, ONE_SET), funds=funds))
    assert s.suggested == 4 and s.real_lots == 0 and "unknown" in s.real_reason


def test_new_terms_never_raise_a_size_395():
    import random

    rng = random.Random(395)
    for _ in range(300):
        ml, c, risk = -rng.uniform(500, 30000), rng.uniform(10, 300), rng.uniform(0, 90_000)
        book = {m: -rng.uniform(0, 70_000) for m in tp.SHOCK_MOVES}
        one = {m: rng.uniform(-8000, 2000) for m in tp.SHOCK_MOVES}
        funds = tp.Funds(rng.uniform(0, 4e5), rng.uniform(0, 3e5), rng.uniform(0, 4e5), rng.uniform(0, 9e4))
        old = tp.size(ml, c, 20_000.0, proven=True, book=_book(risk))
        new = tp.size(ml, c, 20_000.0, proven=True, book=_book(risk, tp.Shock(book, one), funds))
        assert new.multiple == old.multiple and new.suggested <= old.suggested and new.real_lots <= old.real_lots
        assert new.real_lots <= new.suggested
        if new.suggested:  # the accepted book stays within the shock budget at every move
            assert all(book[m] + new.suggested * one[m] >= -0.06 * CAPITAL - 1e-6 for m in tp.SHOCK_MOVES)


# --------------------------------------------------------------------------- the verdict: real-money blockers
def _decide(**kw):
    from finresearch.signals import tradeplan as tpl

    return tpl.decide([], ML, COSTS, 20_000.0, "proven", **kw)


def test_a_full_shock_budget_reads_wait_with_its_own_check_395():
    d = _decide(book=_book(shock=tp.Shock({**FLAT, M10: -59000.0}, ONE_SET), funds=tp.Funds(1e6, 0.0, 0.0, 1.0)),
                mu_lo=100.0)  # fmt: skip
    assert d.action == "WAIT" and "Book within its common-shock budget" in d.failed and d.size.real_lots == 0


def test_unknown_margin_turns_enter_to_wait_395():
    d = _decide(book=_book(shock=tp.Shock(FLAT, ONE_SET), funds=tp.Funds(None, 0.0, 0.0, 1.0)), mu_lo=100.0)
    assert d.action == "WAIT" and "Margin within its cap (real book)" in d.failed and d.size.real_lots == 0
    assert d.size.suggested == 4  # the paper size is kept
    assert any("unknown" in r for r in d.reasons)


def test_known_margin_room_keeps_enter_395():
    d = _decide(book=_book(shock=tp.Shock(FLAT, ONE_SET), funds=tp.Funds(1e6, 0.0, 0.0, 1000.0)), mu_lo=100.0)
    assert d.action == "ENTER" and d.size.real_lots == 4


@pytest.mark.parametrize("mu_lo", [None, 0.0, -50.0])
def test_kelly_is_zero_while_the_expectancys_lower_bound_is_not_positive_395(mu_lo):
    d = _decide(book=_book(shock=tp.Shock(FLAT, ONE_SET), funds=tp.Funds(1e6, 0.0, 0.0, 1000.0)), mu_lo=mu_lo)
    assert d.action == "WAIT" and "Kelly ceiling above zero" in d.failed and d.size.real_lots == 0


# --------------------------------------------------------------------------- the shock P&L (#394's engine)
def _b76(right, f, k, t, r, v):
    from statistics import NormalDist

    n = NormalDist()
    d1 = (math.log(f / k) + v * v * t / 2) / (v * math.sqrt(t))
    d2 = d1 - v * math.sqrt(t)
    c = math.exp(-r * t) * (f * n.cdf(d1) - k * n.cdf(d2))
    return c if right == "call" else c - math.exp(-r * t) * (f - k)


def test_shock_pnls_reprice_one_session_later_with_the_spot_vol_response_395():
    from datetime import date, datetime

    from finresearch.fincalc import book_scenarios as bs
    from finresearch.fincalc.dates import IST

    now = datetime(2026, 10, 22, 15, 30, tzinfo=IST)  # Thursday close; one session later = Friday 23-Oct 15:30
    exp, r, f, k, v = date(2026, 11, 26), 0.065, 24000.0, 23000.0, 0.15
    t0, t1 = 35 / 365, 34 / 365
    mark = _b76("put", f, k, t0, r, v)
    leg = bs.BookLeg("NIFTY", exp, "put", k, -65, mark, "mid")
    m = bs.Market(now, r, {("NIFTY", exp): f})
    got, reasons = bs.shock_pnls([leg], m, bs.horizon_clock(now, 1))
    assert reasons == ()
    for mv in tp.SHOCK_MOVES:
        # independent: F' = F (1 + m) e^{−r (T0 − T1)}; IV + 1.04 points per 1 % fall, − 0.76 per 1 % rise
        f1 = f * (1 + mv) * math.exp(-r * (t0 - t1))
        v1 = v + (-1.04 * mv * 100 if mv < 0 else -0.76 * mv * 100) / 100
        assert got[mv] == pytest.approx((_b76("put", f1, k, t1, r, v1) - mark) * -65, rel=1e-6)
    # P&L is linear in lots: three sets lose three times one (dL/dn = L_1)
    three, _ = bs.shock_pnls([bs.BookLeg("NIFTY", exp, "put", k, -195, mark, "mid")], m, bs.horizon_clock(now, 1))
    assert all(three[mv] == pytest.approx(3 * got[mv], rel=1e-9) for mv in tp.SHOCK_MOVES)
    # no legs: no loss at any move (known, not unknown)
    assert bs.shock_pnls([], m, bs.horizon_clock(now, 1)) == ({mv: 0.0 for mv in tp.SHOCK_MOVES}, ())


def test_a_leg_without_a_beta_or_a_mark_makes_the_shock_unknown_395():
    from datetime import date, datetime

    from finresearch.fincalc import book_scenarios as bs
    from finresearch.fincalc.dates import IST

    now, exp = datetime(2026, 10, 22, 15, 30, tzinfo=IST), date(2026, 11, 26)
    m = bs.Market(now, 0.065, {("BANKNIFTY", exp): 52000.0, ("NIFTY", exp): 24000.0})
    bank = bs.BookLeg("BANKNIFTY", exp, "put", 50000, -30, 300.0, "mid")
    got, reasons = bs.shock_pnls([bank], m, bs.horizon_clock(now, 1))
    assert got is None and any("beta" in x for x in reasons)
    got, reasons = bs.shock_pnls([bs.BookLeg("NIFTY", exp, "put", 23000, -65, None, None)], m, bs.horizon_clock(now, 1))
    assert got is None and reasons
