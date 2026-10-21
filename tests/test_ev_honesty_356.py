"""#356 (P0.6): EV honesty. Step 1, committed before any change: a golden test documenting what today's
"EV after costs (real-world)" gate actually tests on the recorded NIFTY chain.

The gate passes when EV_rw >= -EV_TOLERANCE x costs, but EV_rw already subtracts the entry costs and costs = entry +
expected exercise STT, so the entry costs cancel: EV_rw + costs = e^(-rT) E_RV[gross] - premium - (e^(-rT) E_RV[STT_ex]
- E_RN[STT_ex]). The gate is therefore 'IV at the strikes above RV': it ignores the costs entirely."""

from __future__ import annotations

import math
from datetime import date

import pytest
from test_fno_signal import chain

from finresearch.signals import fno as fs

EV_GATE = "EV after costs (real-world)"
# the registered NIFTY put credit spread's shape on the fixture chain
PCS = "sell:put:22600:1,buy:put:22400:1"


def closes_at(rv: float, n: int = 84) -> list[float]:
    """Closes whose 20-day realised volatility is `rv`: daily log returns alternating ±d (mean 0 over an even
    window), d = rv/√252·√(19/20) so the sample standard deviation over 20 returns × √252 is exactly rv."""
    d = rv / math.sqrt(252) * math.sqrt(19 / 20)
    out = [22780.0]
    for i in range(n - 1):
        out.append(out[-1] * math.exp(d if i % 2 == 0 else -d))
    return out


def run(legs: str, rv: float, brokerage: float = 20, preset: str | None = None):
    lg = fs.preset_legs(chain(), preset) if preset else fs.parse_legs(legs)
    a = fs.analyse(chain(), 65, lg, today=date(2026, 9, 29), closes=closes_at(rv), iv_series=[], capital=10_000_000,
                   max_loss_pct=5, brokerage=brokerage)  # fmt: skip
    return a, fs.assess(a)


def test_golden_todays_ev_gate_ignores_costs_and_tracks_rv_against_iv():
    """The golden test as first committed (d56ce43) asserted, on the recorded put spread, that the old gate passed at
    RV 8% and 10% and failed at 12-16% whatever the costs (₹64 or ₹4,737: EV_rw + costs identical, the entry costs
    cancelling), i.e. it tested 'IV at the strikes above RV'. After #356 that gate is gone: the same sweep leaves the
    failed checks unchanged across RV and costs, and the scenario EV carries the costs one-for-one."""
    assert run(PCS, 0.12)[0]["vols"]["realised_20d"] == pytest.approx(0.12, abs=1e-4)
    base = None
    for rv in (0.08, 0.10, 0.12, 0.14, 0.16):
        cheap, (_, _, f_cheap) = run(PCS, rv, brokerage=20)
        dear, (_, _, f_dear) = run(PCS, rv, brokerage=2000)
        assert dear["costs"]["total_to_expiry"] > 70 * cheap["costs"]["total_to_expiry"]
        assert EV_GATE not in f_cheap and EV_GATE not in f_dear
        hard = [x for x in f_cheap if x != "IV context fits the strategy"]  # context of ad hoc legs (#355)
        base = hard if base is None else base
        assert hard == base == [x for x in f_dear if x != "IV context fits the strategy"]


# ------------------------------------------------------------------------------------------------ after the change
LIQ = "Bid/ask spreads within 10% of the premium"
EV_INFO = "EV after costs and spreads (model, not validated)"


def _half(c, right: str, strike: float) -> float:
    q = getattr(next(r for r in c.rows if float(r.strike) == strike), right)
    return (float(q.ask) - float(q.bid)) / 2


def test_the_fair_price_identity_with_own_strike_ivs_and_spreads():
    # (a) legs at their own-strike IV mids: EV_Q = −(statutory costs + half-spreads), within ₹1
    from finresearch.fincalc import charges as ch
    from finresearch.fincalc import options as o

    a, _ = run(PCS, 0.12)
    c = chain()
    halves = {k: _half(c, "put", k) for k in (22600, 22400)}
    entry_spread = 65 * sum(halves.values())  # Σ|q|·(ask − bid)/2: 65 × (0.15 + 0.525) = ₹43.875
    assert entry_spread == pytest.approx(43.875)
    hs = a["costs"]["half_spreads"]
    assert hs["entry"] == pytest.approx(entry_spread, abs=0.01) and hs["p_early_exit"] == 1.0
    assert hs["exit_expected"] == pytest.approx(entry_spread, abs=0.01) and hs["source"] == "bid/ask"
    # the expected exercise STT on the bought 22400 put at its own IV (mid 53.125), computed here independently
    t, r = a["t_years"], a["rate"]
    iv = o.implied_vol("put", 53.125, a["spot"], 22400, t, r)
    stt = float(ch.rates_as_of(date(2026, 10, 6))["stt_option_exercise"].rate)
    stt_q = stt * 65 * o.bs_price("put", a["spot"], 22400, t, r, iv)  # discounted E_Q[STT on exercise]
    assert a["costs"]["expected_exercise_stt"] == pytest.approx(stt_q, abs=0.05)
    c_stat = a["costs"]["entry"]["total"] + stt_q
    assert a["risk_neutral"]["ev"] == pytest.approx(-(c_stat + 2 * entry_spread), abs=1.0)


def test_model_ev_is_informational_and_cannot_change_a_verdict():
    # (b) the condor's verdict is identical at RV 10%, 12% and 14%; the RV EV is shown, labelled, and scores 0
    from finresearch.signals import tradeplan as tpl

    seen = []
    for rv in (0.10, 0.12, 0.14):
        a, (_, factors, failed) = run("", rv, preset="iron_condor")
        d = tpl.decide(failed, a["max_loss"], a["costs"]["entry"]["total"], 500_000, "proven")
        # the card's verdict (signals.tradeplan.decide); the IV/RV context of ad hoc legs is context, never a gate (#355)
        seen.append((d.action, tuple(d.failed)))
        assert EV_GATE not in failed
        ev = next(f for f in factors if f.name == EV_INFO)
        assert ev.contribution == 0 and "scenario, not a forecast" in ev.explanation
        assert a["model_ev"]["label"] == "model EV - not validated"
        lo, hi = a["model_ev"]["range"]
        assert lo <= a["risk_neutral"]["ev"] <= hi and lo <= a["real_world"]["ev"] <= hi
    assert len(set(seen)) == 1 and seen[0][0] == "ENTER"  # proven evidence, every gate passing


def test_costs_now_count_in_the_real_world_scenario():
    # the old gate cancelled the entry costs; the scenario EV now moves one-for-one with them
    cheap, _ = run(PCS, 0.12, brokerage=20)
    dear, _ = run(PCS, 0.12, brokerage=2000)
    extra = dear["costs"]["entry"]["total"] - cheap["costs"]["entry"]["total"]
    assert dear["real_world"]["ev"] == pytest.approx(cheap["real_world"]["ev"] - extra, abs=0.5)
    # and the scenario prices the legs at their fills (bid for the sold put, ask for the bought one), not at LTP
    assert [x["fill"] for x in cheap["legs"]] == [91.7, 53.65]


def _wide(c, right: str, strike: float, bid: float, ask: float):
    from decimal import Decimal

    q = getattr(next(r for r in c.rows if float(r.strike) == strike), right)
    q.bid, q.ask = Decimal(str(bid)), Decimal(str(ask))
    return c


def _assess_on(c, legs: str):
    a = fs.analyse(c, 65, fs.parse_legs(legs), today=date(2026, 9, 29), closes=closes_at(0.12), iv_series=[],
                   capital=10_000_000, max_loss_pct=5, brokerage=20)  # fmt: skip
    return a, fs.assess(a)


def test_the_liquidity_gate_binds_spreads():
    from finresearch.signals import tradeplan as tpl

    a, (_, _, failed) = _assess_on(chain(), PCS)
    share = a["costs"]["half_spreads"]["share_of_premium"]
    assert LIQ not in failed and share == pytest.approx(43.875 / (38.725 * 65), abs=1e-4)
    # the sold put quoted 88-96 and the bought one 50-57: Σ half-spreads 65 × (4 + 3.5) = ₹487.5 > 10% of the
    # ₹2,502.5 credit at mids (38.5 × 65): fails, so even a proven recipe reads WAIT, never ENTER
    c = _wide(_wide(chain(), "put", 22600, 88, 96), "put", 22400, 50, 57)
    a, (_, factors, failed) = _assess_on(c, PCS)
    assert LIQ in failed
    assert tpl.decide(failed, a["max_loss"], a["costs"]["entry"]["total"], 500_000, "proven").action == "WAIT"
    # unknown bid/ask is never a pass
    c = _wide(chain(), "put", 22400, 0, 0)
    a, (_, factors, failed) = _assess_on(c, PCS)
    assert LIQ in failed and a["costs"]["half_spreads"]["source"] == "slippage tiers (no live bid/ask)"
    assert next(f for f in factors if f.name == LIQ).value is None


def test_the_direction_table_separates_the_pricing_residual_from_ev():
    # (c) at the market's own probabilities the EV is the pricing residual (ATM-IV lognormal vs each leg's IV and last
    # price) minus costs: it is reported as such, beside the fair-price EV at own-strike IVs, never as edge
    from finresearch.direction import decide, models, specs
    from finresearch.fincalc import options as o

    c = chain()
    implied = {e: models.implied_p(14.0, 5, e) for e in specs.EVENTS}
    d = decide.decide(c, 75, today=date(2026, 9, 29), horizon_day=date(2026, 10, 6), h=5, iv_30=14.0,
                      probs_model=implied, accepted=False, rate=0.065, brokerage=20.0)  # fmt: skip
    rows = [r for r in d["candidates"] if r["status"] != "unavailable"]
    assert rows
    t = 7 / 365
    for r in rows:
        sign = {"buy": 1, "sell": -1}
        fair = sum(sign[x["side"]] * 75 * (o.bs_price(x["right"], 22780.25, x["strike"], t, 0.065, x["iv"] / 100)
                                           - x["price"]) for x in r["legs"]) - r["costs"]  # fmt: skip
        assert r["ev_fair"] == pytest.approx(fair, abs=0.5)
        assert r["pricing_residual"] == pytest.approx(
            r["ev_implied"] - r["ev_fair"], abs=0.02
        )  # each rounded to paise
        assert r["status"] == "fails"  # no accepted model: a residual never passes the gate


def test_a_held_position_exits_only_on_a_hard_check_now():
    # before #356 a held position whose real-world EV fell below −costs read EXIT; the model EV is no longer a gate, so
    # it reads HOLD unless a hard check fails (listed in the PR as a decision for the owner)
    a, _ = run("", 0.30, preset="iron_condor")  # RV 30%: the scenario EV is deeply negative
    assert a["real_world"]["ev"] < -a["costs"]["total_to_expiry"]
    assert fs.assess(a, held=True)[0] == "HOLD"
    wide = fs.analyse(chain(), 65, fs.preset_legs(chain(), "strangle"), today=date(2026, 9, 29), closes=closes_at(0.30),
                      iv_series=[], capital=10_000_000, max_loss_pct=5, brokerage=20)  # fmt: skip
    assert fs.assess(wide, held=True)[0] == "EXIT"  # undefined risk is still a hard exit
