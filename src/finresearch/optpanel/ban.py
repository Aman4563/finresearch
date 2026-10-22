"""The F&O ban period from market-wide position limit utilisation (P1.12, #374; PREREG.md §3), pure.

NSE's rule for a stock in F&O: when its combined open interest across exchanges exceeds 95 % of the market-wide
position limit (MWPL) at the end of a day, it enters the ban period from the next session (no new positions, only
reductions), and it leaves the ban only once the combined OI falls below 80 % of MWPL. SEBI's circular
SEBI/HO/MRD/TPD-1/P/CIR/2025/79 of 29-May-2025 changed how OI and MWPL are measured (delta-based OI, MWPL linked to
free float and turnover); the 95 % / 80 % thresholds are read here as unchanged by it [U: thresholds per NSE's
long-standing ban rule; not re-read in the circular's text for this change]. params.BAN_ENTER / BAN_EXIT hold them.

Because the rule is hysteretic, a stock's state is computed in session order, never from one day's ratio: between
80 % and 95 % it keeps yesterday's state, which is unknown until a day decides it and again after a missing file.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date

from finresearch.optpanel import params


def states(us: Iterable[tuple[date, float | None]]) -> list[tuple[date, bool | None]]:
    """For each (file day D, utilisation u_D or None when unknown), in session order: whether the stock is banned
    for the session after D (True / False / None = unknown)."""
    out: list[tuple[date, bool | None]] = []
    state: bool | None = None
    for d, u in us:
        if u is None:
            state = None
        elif u > params.BAN_ENTER:
            state = True
        elif u < params.BAN_EXIT:
            state = False
        out.append((d, state))
    return out


def compare(derived: Mapping[str, bool | None], notice: Iterable[str]) -> dict[str, object]:
    """The derived states for one trade day against NSE's ban notice for that day. `match` only when every banned
    stock agrees and no state is unknown."""
    n = set(notice)
    got = {
        "agree": sorted(s for s, v in derived.items() if v is True and s in n),
        "missed": sorted(s for s, v in derived.items() if v is False and s in n),
        "extra": sorted(s for s, v in derived.items() if v is True and s not in n),
        "unknown": sorted(s for s, v in derived.items() if v is None),
        "not_in_file": sorted(n - set(derived)),
    }
    got["match"] = not (got["missed"] or got["extra"] or got["unknown"] or got["not_in_file"])
    return got
