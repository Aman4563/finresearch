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
    assert run(PCS, 0.12)[0]["vols"]["realised_20d"] == pytest.approx(0.12, abs=1e-4)
    for rv, passes in ((0.08, True), (0.10, True), (0.12, False), (0.14, False), (0.16, False)):
        cheap, (_, _, f_cheap) = run(PCS, rv, brokerage=20)
        dear, (_, _, f_dear) = run(PCS, rv, brokerage=2000)
        # ₹64 or ₹4,737 of costs: the same answer, and the same EV_rw + costs (the entry costs cancel)
        assert dear["costs"]["total_to_expiry"] > 70 * cheap["costs"]["total_to_expiry"]
        assert (EV_GATE not in f_cheap) is passes and (EV_GATE not in f_dear) is passes
        lhs = [x["real_world"]["ev"] + x["costs"]["total_to_expiry"] for x in (cheap, dear)]
        assert lhs[0] == pytest.approx(lhs[1], abs=0.05)
        # the identity: EV_rw + costs = EV_rw before costs − (discounted E_RV[STT_ex] − E_RN[STT_ex])
        rw, entry = cheap["real_world"], cheap["costs"]["entry"]["total"]
        stt_rw = rw["ev_gross"] - entry - rw["ev"]  # e^(−rT)·E_RV[STT on exercise]
        stt_rn = cheap["costs"]["expected_exercise_stt"]
        assert lhs[0] == pytest.approx(rw["ev_gross"] - stt_rw + stt_rn, abs=0.05)
