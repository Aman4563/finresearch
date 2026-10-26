"""P4.2 Stage 2 (#395): the replay's common-shock inputs on a stored day: the open book's and one lot's P&L at a NIFTY
move, one session later, from the legs' marks on the engine's forward, rolled and discounted at the Book's rate (the
study's registered rule), BANKNIFTY at the worse end of its point-in-time beta band. Expected values are an
independent Black-76 written here."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from finresearch.backtest import engine
from finresearch.backtest.history import Day, Quote
from finresearch.fincalc import beta as fb
from finresearch.riskbudget import shock

R, VOL, F = 0.065, 0.15, 20000.0
D0, D1, EXP = (
    date(2025, 1, 10),
    date(2025, 1, 13),
    date(2025, 2, 13),
)  # Friday, the next session Monday; 34 days


def ncdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def b76(right: str, f: float, k: float, t: float, v: float) -> float:
    d1 = (math.log(f / k) + v * v * t / 2) / (v * math.sqrt(t))
    d2 = d1 - v * math.sqrt(t)
    c = math.exp(-R * t) * (f * ncdf(d1) - k * ncdf(d2))
    return c if right == "call" else c - math.exp(-R * t) * (f - k)


def chain(sym: str = "NIFTY", f: float = F, step: int = 100) -> Day:
    x = Day(D0, sym, spot=None, iv30=15.0)
    x.futures[EXP] = f
    t = (EXP - D0).days / 365
    for k in range(int(f * 0.85), int(f * 1.15), step):
        for right in ("put", "call"):
            p = round(b76(right, f, k, t, VOL), 2)
            x.options[(EXP, right, float(k))] = Quote(EXP, right, float(k), p, p, 500, 10_000, 75)
    return x


def position(x: Day, legs: list[tuple[str, float, str]], units: int) -> engine.Position:
    ls = [engine.Leg(r, k, side, units, x.options[(EXP, r, k)].ref, VOL, x.options[(EXP, r, k)].ref)
          for r, k, side in legs]  # fmt: skip
    return engine.Position(
        "nifty_pcs_m", x.symbol, EXP, D0, 34, units // 75, 75, ls, 0.0, 1.0, 1.0, 0.0, 15.0, 0.5
    )


def expected(x: Day, legs: list[tuple[str, float, int]], m: float, beta: float = 1.0) -> float:
    """Σ q (V' − mark): F' = F (1 + βm) e^{−r h} over h = 3 days, T' = 31/365, σ' = σ + 1.04 vol points per 1 % fall."""
    f = x.futures[EXP]
    f1, t1 = f * (1 + beta * m) * math.exp(-R * 3 / 365), 31 / 365
    dv = (-1.04 * beta * m * 100 if m < 0 else -0.76 * beta * m * 100) / 100
    return sum(q * (b76(r, f1, k, t1, VOL + dv) - x.options[(EXP, r, k)].ref) for r, k, q in legs)


def test_the_books_and_one_lots_shock_one_session_on() -> None:
    x = chain()
    book = [position(x, [("put", 19000.0, "sell"), ("put", 18600.0, "buy")], 150)]
    one = [("call", 21000.0, -75), ("call", 21400.0, 75)]
    l_book, l_one, why = shock.pnls(shock.position_legs(book), shock.legs("NIFTY", EXP, one, x), {"NIFTY": x},
                                    D0, D1, {}, (-0.05, 0.05), R)  # fmt: skip
    assert not why
    for m in (-0.05, 0.05):
        assert l_book[m] == pytest.approx(
            expected(x, [("put", 19000.0, -150), ("put", 18600.0, 150)], m), abs=2.0
        )
        assert l_one[m] == pytest.approx(expected(x, one, m), abs=1.0)
    assert (
        l_book[-0.05] < -5_000 and l_one[0.05] < 0
    )  # the put spread loses on the fall, the call spread on the rise


def test_an_empty_book_loses_nothing_and_a_missing_day_is_unknown() -> None:
    x = chain()
    one = shock.legs("NIFTY", EXP, [("put", 19000.0, -75)], x)
    l_book, l_one, _ = shock.pnls([], one, {"NIFTY": x}, D0, D1, {}, (-0.05,), R)
    assert l_book == {-0.05: 0.0} and l_one[-0.05] < 0
    l_book, l_one, why = shock.pnls([], one, {"NIFTY": None}, D0, D1, {}, (-0.05,), R)
    assert l_one is None and any("forward" in w for w in why)


def test_banknifty_needs_a_known_beta_and_moves_at_the_worse_end() -> None:
    nifty, bank = chain(), chain("BANKNIFTY", 50000.0, 500)
    one = shock.legs("BANKNIFTY", EXP, [("put", 47500.0, -30), ("put", 46500.0, 30)], bank)
    days = {"NIFTY": nifty, "BANKNIFTY": bank}
    unknown = fb.unknown("BANKNIFTY", "only 40 paired returns (needs 120)")
    _, l_one, why = shock.pnls([], one, days, D0, D1, {"BANKNIFTY": unknown}, (-0.05,), R)
    assert l_one is None and any("beta to NIFTY unknown" in w for w in why)
    band = fb.BetaEstimate("BANKNIFTY", 1.2, 0.05, 1.1, 1.3)
    _, l_one, why = shock.pnls([], one, days, D0, D1, {"BANKNIFTY": band}, (-0.05,), R)
    # a short put spread loses more the further BANKNIFTY falls: the upper end (1.3) binds on a fall
    assert l_one[-0.05] == pytest.approx(expected(bank, [("put", 47500.0, -30), ("put", 46500.0, 30)], -0.05, 1.3),
                                         abs=1.0)  # fmt: skip


def futures(start: date, n: int, beta: float, late: float | None = None) -> dict[str, list]:
    """n weekday sessions of NIFTY and BANKNIFTY front futures (one contract far out); BANKNIFTY's log return is
    `beta` x NIFTY's, NIFTY alternating +1 % and −0.5 %; after the first 150 sessions it is `late` x NIFTY's."""
    out: dict[str, list] = {"NIFTY": [], "BANKNIFTY": []}
    d, a, b, i = start, 20000.0, 50000.0, 0
    exp = start + timedelta(days=3 * 365)
    while i < n:
        if d.weekday() < 5:
            out["NIFTY"].append((d, exp, a))
            out["BANKNIFTY"].append((d, exp, b))
            r = 0.01 if i % 2 else -0.005
            a, b = a * math.exp(r), b * math.exp((late if late and i >= 150 else beta) * r)
            i += 1
        d += timedelta(days=1)
    return out


def test_the_beta_is_the_registered_estimate_as_of_the_session() -> None:
    futs = futures(date(2022, 1, 3), 200, 1.5, late=3.0)
    days = sorted(d for d, _, _ in futs["NIFTY"])
    early = shock.beta_as_of(
        futs, "BANKNIFTY", days[100]
    )  # 100 returns so far: fewer than the 120 registered
    assert not early.known and "only 100 paired returns" in early.reason
    est = shock.beta_as_of(futs, "BANKNIFTY", days[149])  # every return so far is exactly 1.5 x NIFTY's
    assert est.known and est.beta == pytest.approx(1.5) and est.as_of == days[149]
    assert est.hi == pytest.approx(1.5) and est.n == 149  # the later 3.0 regime is not read
