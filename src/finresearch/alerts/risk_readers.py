"""P4.5 (#398): what the risk alerts read. Each reading names the series it read; an input that cannot be read is
None with the reason (alerts.risk raises a "can't check" alert), never 0 or ok. Local only: the F&O book and the
funds are personal data and never reach a model. Alerts only: nothing here trades.

Book level (`book_readings`, pure): the open book's common-shock P&L (fnobook.scenarios.book_shock, at
fincalc.tradeplan.SHOCK_MOVES one session ahead) against the shock budget (limit_shock, hysteresis) and against the
book loss limit (stress_breach); the margin cap in use (fincalc.tradeplan.margin_utilisation with no new trade, over
MARGIN_UTIL_MAX; only with Groww connected); expected shortfall (P3.4) and stressed margin (P4.3) are not built, so
while a book is open they read "can't check". Stored series (`series_readings`, `leg_readings`) are in the database.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from finresearch.alerts.risk import Reading
from finresearch.fincalc import tradeplan as tp

Row = tuple[str, str, Reading]  # (rule id, instrument, reading)
SHOCK_SERIES = "the open F&O book (paper + Groww) repriced at fincalc.tradeplan.SHOCK_MOVES one session ahead"
FUNDS_SERIES = "Groww funds from the last sync (fnobook.service.stored_funds)"


def book_readings(shock: Mapping[float, float] | None, shock_notes: Sequence[str], capital: float | None,
                  funds: Mapping[str, float] | None, funds_note: str | None, *, book_open: bool) -> list[Row]:  # fmt: skip
    out: list[Row] = []
    why = "; ".join(shock_notes) or "the open book could not be repriced"
    if not capital or capital <= 0:
        miss = "F&O capital is not set on the profile, so the book has no budget to measure against"
        out += [("limit_shock", "BOOK", Reading(None, SHOCK_SERIES, miss)),
                ("stress_breach", "BOOK", Reading(None, SHOCK_SERIES, miss))]  # fmt: skip
    elif shock is None or any(v is None for v in shock.values()):
        out += [("limit_shock", "BOOK", Reading(None, SHOCK_SERIES, why)),
                ("stress_breach", "BOOK", Reading(None, SHOCK_SERIES, why))]  # fmt: skip
    else:
        worst = min(shock, key=lambda m: shock[m])
        loss = max(0.0, -shock[worst])
        for rid, pct, name in (("limit_shock", tp.SHOCK_BUDGET_PCT, "common-shock budget"),
                               ("stress_breach", tp.BOOK_BUDGET_PCT, "book loss limit")):  # fmt: skip
            limit = capital * pct / 100
            out.append((rid, "BOOK", Reading(loss / limit, SHOCK_SERIES, f"worst loss ₹{loss:,.0f} at a {worst:+.0%} "
                        f"NIFTY move vs the ₹{limit:,.0f} {name} ({pct:g}% of F&O capital)")))  # fmt: skip
    if book_open or shock is None:
        out += [("es", "BOOK", Reading(None, "no expected-shortfall series", "the book's expected shortfall is not "
                                       "computed yet (P3.4), so its limit cannot be checked")),
                ("margin_stress", "BOOK", Reading(None, "no stressed-margin series", "stressed margin is not "
                                                  "computed yet (P4.3), so the headroom cannot be checked"))]  # fmt: skip
    if funds is not None:
        u = tp.margin_utilisation(funds["margin_used"], 0.0, funds["cash"], funds["collateral"])
        out.append(("margin", "BOOK", Reading(u / tp.MARGIN_UTIL_MAX, FUNDS_SERIES, f"margin in use ₹"
                    f"{funds['margin_used']:,.0f}: utilisation {u:.0%} of a {tp.MARGIN_UTIL_MAX:.0%} cap")))  # fmt: skip
    elif funds_note and "not connected" not in funds_note:
        out.append(("margin", "BOOK", Reading(None, FUNDS_SERIES, funds_note)))
    return out
