"""The VRP report (P3.6, #389; vrp/run.py) on a small synthetic history: split by period and era, every variant and
both charge cases present, the fractional hedge labelled, nothing adopted."""

from datetime import date, timedelta

from finresearch.fincalc import realvol as rv
from finresearch.vrp import collect as co
from finresearch.vrp import run
from finresearch.vrp import study as st


def test_report_splits_periods_and_labels_the_benchmark() -> None:
    d0 = date(2022, 1, 27)
    days, px = [], 17000.0
    for i in range(70):
        d = d0 + timedelta(i)
        o, c = px * 1.001, px * (1.004 if i % 2 else 0.997)
        days.append(
            st.Day(d, rv.Bar(d, o, max(o, c) * 1.002, min(o, c) * 0.998, c, px), 0.03, None, 0.031, 17.0)
        )
        px = c
    m = co.Mark(d0, 17000.0, 300.0, 290.0, 5000, 5000)
    tr = co.Trade(d0, date(2022, 2, 24), 17000.0, 50, (m,), 17100.0, ("call", "put"))
    x = run.Inputs(days, {d0, d0 + timedelta(28)}, days[-1].day, [tr], [], {d0: 0.5}, {d0: None})
    out = run.report(x, draws=50, seed=1)
    assert set(out["periods"]) == {"seen"} and set(out["gate"]) == {"2022-2026"}
    p = out["periods"]["seen"]
    assert p["windows"] == 2 and p["primary"]["n"] == 2
    assert p["collect"]["dated"]["straddle"]["none"]["n"] == 1
    assert p["collect"]["dated"]["straddle"]["fractional"]["label"] == run.FRACTIONAL_LABEL
    assert (
        p["collect"]["today"]["straddle"]["none"]["mean_net"]
        < p["collect"]["dated"]["straddle"]["none"]["mean_net"]
    )
    assert p["z"]["mf"]["n"] == 1 and p["z"]["atm"]["unknown"] == 1
    assert "not adopted" in out["verdict"]


def test_counted_reasons_carry_no_price_strike_or_date() -> None:
    # a vault read writes these keys: a forward, strike range or date in them would be a per-day vault figure
    d0 = date(2022, 1, 27)
    bad = st.Day(d0, None, None, "near term 2022-02-24: the traded strikes 16500-17500 do not cover (16712.3-17311.5)",
                 None, None)  # fmt: skip
    old = co.Trade(date(2008, 1, 31), date(2008, 2, 28), 5000.0, 50,
                   (co.Mark(date(2008, 1, 31), 5010.0, 90.0, 80.0, 500, 500),), 5100.0, ("call", "put"))  # fmt: skip
    x = run.Inputs([bad], {d0}, d0, [old], [], {}, {})
    out = run.report(x, draws=20, seed=1)
    keys = list(out["periods"]["seen"]["primary"]["unknown_reasons"])
    assert keys == ["near term #: the traded strikes # do not cover (#)"]
    rep = run.report(run.Inputs([st.Day(date(2008, 1, 31), None, 0.04, None, None, None)], {date(2008, 1, 31)},
                                date(2008, 1, 31), [old], [], {}, {}), draws=20, seed=1)  # fmt: skip
    for k in rep["periods"]["vault"]["collect"]["dated"]["straddle"]["none"]["unknown_reasons"]:
        assert not any(ch.isdigit() for ch in k), k
