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
from datetime import date
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


async def risk_free_on(day: date, t_years: float, *, before: bool = False) -> dict[str, Any]:
    """The dated rate for `day` from the stored FBIL curves (refseries.rate_lookup.par_rate_on, #366), in the shape
    of risk_free() plus `known`. Unlike risk_free() it has no fallback: a day without a stored curve returns rate
    None with the reason, never FIXED_RATE. Callers on historical dates should switch to this (see #366's PR)."""
    from finresearch.db import session_scope
    from finresearch.refseries.rate_lookup import par_rate_on

    with session_scope() as s:
        r = par_rate_on(s, day, t_years, before=before)
    return r | {"fallback": False}


# --------------------------------------------------------------------------- dated rate with a labelled fallback
FALLBACK_SOURCE = "fallback 6.5 %, no curve"


def fallback(why: str) -> dict[str, Any]:
    """FIXED_RATE as an answer that says it is a fallback and why (#366/#385: never a silent 6.5 %)."""
    return {"known": False, "rate": FIXED_RATE, "par_yield": None, "tenor_years": None, "as_of": None,
            "fallback": True, "source": FALLBACK_SOURCE, "note": f"{why}; the fixed 6.5 % is used"}  # fmt: skip


def or_fallback(answer: dict[str, Any]) -> dict[str, Any]:
    """A dated answer (refseries.rate_lookup) as is when known, else the labelled fallback with its reason."""
    if answer.get("known") and answer.get("rate") is not None:
        return answer | {"fallback": False}
    return fallback(answer.get("note") or "no FBIL curve")


def rate_on(s: Any, day: date, t_years: float, *, before: bool = False) -> dict[str, Any]:
    """The stored FBIL par rate for `day` at `t_years` (refseries.rate_lookup.par_rate_on, in the caller's session),
    or the labelled 6.5 % fallback when no curve is known. For non-registered callers only."""
    from finresearch.refseries.rate_lookup import par_rate_on

    return or_fallback(par_rate_on(s, day, t_years, before=before))


async def rate_for(day: date, t_years: float, *, before: bool = False) -> dict[str, Any]:
    """rate_on in its own session; a rate store that cannot be read is the labelled fallback, with the reason."""
    from finresearch.db import session_scope

    try:
        with session_scope() as s:
            return rate_on(s, day, t_years, before=before)
    except Exception as e:  # the reason is shown with the fallback
        return fallback(f"the stored FBIL rates could not be read ({type(e).__name__})")


def given(rate: float, why: str) -> dict[str, Any]:
    """A rate a caller fixed on purpose (a registered setting, or a value the user typed), labelled as such."""
    return {"known": True, "rate": rate, "par_yield": None, "tenor_years": None, "as_of": None, "fallback": False,
            "source": why, "note": why}  # fmt: skip


def label(info: dict[str, Any]) -> str:
    """One line naming the rate used: 'FBIL G-sec par yield, 0.25-year point, curve of 2026-10-20: 5.91 %
    continuous' or 'fallback 6.5 %, no curve: <reason>'."""
    if info.get("fallback") and info.get("source") == FALLBACK_SOURCE:
        return f"{FALLBACK_SOURCE}: {info.get('note')}"
    out = f"{info.get('source')}: {float(info['rate']):.2%} continuous"
    return f"{out} ({info.get('note')})" if info.get("fallback") and info.get("note") else out


def effective_annual(info: dict[str, Any]) -> float:
    """The answer as an effective annual rate (1 + y/2)^2 - 1 from its half-yearly par yield (the convention of
    portfolio.analytics.risk_free_from_curve); the fallback stays the stated 6.5 % a year the app used before."""
    y = info.get("par_yield")
    if info.get("fallback"):
        return FIXED_RATE
    return float(info["rate"]) if y is None else (1 + float(y) / 2) ** 2 - 1
