"""Market-context arithmetic (#317, fincalc.market_context): averages and slope, Wilder's ADX, the regime label and the
open-interest build-up classes. Expected values by hand."""

from __future__ import annotations

import pytest

from finresearch.fincalc import market_context as mc


def test_sma_slope_and_distance():
    closes = [float(x) for x in range(1, 31)]  # 1..30
    assert mc.sma(closes, 20) == pytest.approx(sum(range(11, 31)) / 20)  # 20.5
    assert mc.sma(closes, 50) is None  # too few closes: unknown, never 0
    # SMA20 now 20.5, ten sessions ago 10.5: +95.238…%
    assert mc.sma_slope(closes, 20) == pytest.approx((20.5 / 10.5 - 1) * 100)
    assert mc.distance(22.55, 20.5) == pytest.approx(10.0)


def test_adx_of_a_steady_rise_is_100_and_unknown_without_bars():
    close = [100.0 + i for i in range(40)]
    high, low = [c + 1 for c in close], [c - 1 for c in close]
    a = mc.adx(high, low, close)
    # every day: +DM 1, −DM 0, TR 2 → +DI 50, −DI 0, DX 100 → ADX 100
    assert a["adx"] == pytest.approx(100.0) and a["plus_di"] == pytest.approx(50.0) and a["minus_di"] == 0
    assert mc.adx(high[:20], low[:20], close[:20]) is None  # fewer than 2n + 1 bars


def test_adx_of_a_zigzag_is_low():
    close = [100.0 + (1 if i % 2 else -1) for i in range(60)]
    a = mc.adx([c + 1 for c in close], [c - 1 for c in close], close)
    assert a["adx"] < 20


def test_regime_labels():
    assert mc.regime(110, 105, 100, 0.5, 30) == {"trend": "uptrend", "strength": "trending"}
    assert mc.regime(90, 95, 100, -0.5, 15) == {"trend": "downtrend", "strength": "weak"}
    assert mc.regime(101, 105, 100, 0.5, 22)["trend"] == "range / mixed"
    assert mc.regime(101, None, 100, None, None) == {"trend": "unknown", "strength": "unknown"}


@pytest.mark.parametrize(("dp", "doi", "want"), [(5, 100, "long build-up"), (-5, 100, "short build-up"),
                                                 (5, -100, "short covering"), (-5, -100, "long unwinding"),
                                                 (0, 100, "no change"), (None, 100, "unknown"), (5, None, "unknown")])  # fmt: skip
def test_buildup_classes(dp, doi, want):
    assert mc.buildup(dp, doi) == want


def test_pcr():
    assert mc.pcr(150.0, 100.0) == 1.5 and mc.pcr(10.0, 0.0) is None and mc.pcr(None, 5.0) is None
