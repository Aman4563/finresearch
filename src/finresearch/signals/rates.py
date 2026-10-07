"""The risk-free rate for option maths, from FBIL's G-sec par yield curve (#244).

fincalc.options prices with continuous compounding; FBIL publishes par yields compounded half-yearly (G-secs pay
coupons every six months, adapters/fbil.py). The rate used is the par yield at the option's time to expiry, converted
r = 2·ln(1 + y/2) (the continuously compounded rate equal to a half-yearly rate y). FBIL's shortest tenor is
0.25 years, so a weekly or monthly option uses that point (the curve is flat below it).

Fallbacks, each stated in `source` and `note`: FBIL unreachable → its dated 23-Sep-2026 curve (adapters.fbil.
FALLBACK_CURVE, signals.bond's cache); no curve at all → the fixed 6.5 % this app used before (FIXED_RATE).
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Any

FIXED_RATE = 0.065  # the earlier fixed default (continuous), kept only as the last fallback
SHORTEST_TENOR = 0.25  # FBIL's first par-yield tenor (years)
# () -> adapters.fbil.ParCurve | None; replaced in tests (conftest), so no test reaches FBIL
CURVE: Callable[[], Awaitable[Any]] | None = None


def continuous_from_half_yearly(y: float) -> float:
    """The continuously compounded rate equal to a half-yearly compounded annual rate `y` (fractions)."""
    return 2 * math.log1p(y / 2)


async def risk_free(t_years: float) -> dict[str, Any]:
    """{"rate" (continuous, fraction), "par_yield", "tenor_years", "as_of", "fallback", "source", "note"}."""
    from finresearch.signals.bond import _par_curve

    try:
        curve = await (CURVE or _par_curve)()
    except Exception:
        curve = None
    if curve is None or not curve.points:
        return {"rate": FIXED_RATE, "par_yield": None, "tenor_years": None, "as_of": None, "fallback": True,
                "source": "fixed 6.5 % (fallback)",
                "note": "FBIL's G-sec curve was unavailable, so the fixed 6.5 % default is used"}  # fmt: skip
    tenor = max(float(t_years), SHORTEST_TENOR)
    y = float(curve.par_yield(tenor))
    src = f"FBIL G-sec par yield, {tenor:g}-year point, {curve.as_of:%d-%b-%Y}"
    note = (f"{y:.2%} half-yearly → {continuous_from_half_yearly(y):.2%} continuous"
            + (f"; expiry is under {SHORTEST_TENOR:g} years, FBIL's shortest tenor, so that point is used"
               if t_years < SHORTEST_TENOR else ""))  # fmt: skip
    if curve.fallback:
        src = f"FBIL fallback curve of {curve.as_of:%d-%b-%Y} (FBIL unreachable)"
        note += "; " + "; ".join(curve.notes or ["FBIL could not be reached: a dated curve is used"])
    return {"rate": continuous_from_half_yearly(y), "par_yield": y, "tenor_years": tenor,
            "as_of": curve.as_of.isoformat(), "fallback": bool(curve.fallback), "source": src, "note": note}  # fmt: skip


def user_rate(rate: float) -> dict[str, Any]:
    return {"rate": rate, "par_yield": None, "tenor_years": None, "as_of": None, "fallback": False,
            "source": "your input", "note": "the risk-free rate you supplied"}  # fmt: skip
