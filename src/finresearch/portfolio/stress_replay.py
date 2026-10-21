"""Historical stress replay on today's holdings (#345): five dated market episodes replayed day by day on the
current positions and their values. Pure arithmetic here; `load` (below) gathers the price paths.

Each episode is a window [start, end]. The replay starts at the market's last close before `start` (the base, every
holding at today's value) and runs through `end`, then on for up to RECOVERY_DAYS more so a recovery date can be
found. Each holding gets one daily return path, chosen in this order (the first that has data wins):

1. its own closes or NAVs through the window (split/bonus factors applied; a one-day move beyond ±JUMP with no recorded
   action rejects the path: it is probably an unrecorded split, never replayed as a crash);
2. funds only: its category index from `index_eod` × its beta to that index (last year of NAVs);
3. equity (stocks, equity ETFs and funds): beta × the market path, the beta estimated over the last year against
   NIFTYBEES (portfolio.analytics uses the same 1-year beta);
4. listed bonds: -modified duration × the daily change in yield, where a yield series exists for the window;
5. otherwise **unmapped**: listed with its value and the reason, left out of the loss and never counted as a 0 % move.

The market path is the Nifty 50 close from `index_eod` (NSE's daily index file, stored from Mar-2013, #366) when it
covers the window, else NIFTYBEES (the Nifty 50 ETF, traded since 2002) closes from the app's existing price
history. Betas are against NIFTYBEES total return; the index is a price index. Over a window of days to months the
difference (dividends of about 1-1.5 % a year and the ETF's tracking error) is small and is stated, not hidden.

Outputs per episode, on the replayed value of the holdings that could be mapped: the move to the window's end, the
worst day, peak-to-trough inside the window, the recovery date (first day back at that peak) and each holding's ₹
share of the peak-to-trough fall. Instructive, not a forecast: the next crisis will not repeat any of these.

The per-holding return paths stay in `Replay.legs` so the joint view with the F&O book (#397) can add its own legs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from finresearch.portfolio.history import JUMP

MAX_GAP_DAYS = 7  # calendar days between two closes of the market path; more means a hole in the data
STALE_DAYS = 7  # a holding's close may be carried this many calendar days (a thin trading day), no more
MIN_COVERAGE = 0.8  # share of the window's market days a holding's own path must have a close of its own on
RECOVERY_DAYS = 365  # how long after the window the replay keeps looking for a recovery
TOP = 5  # contributors shown


@dataclass(frozen=True)
class Window:
    id: str
    label: str
    start: date
    end: date
    note: str


# Episode dates are the owner's (issue #345); the notes say what happened, from the exchange-reported index moves.
WINDOWS = (
    Window("gfc2008", "Global financial crisis (Lehman), Sep-Nov 2008", date(2008, 9, 1), date(2008, 11, 30),
           "Lehman Brothers failed on 15-Sep-2008 and Indian equities fell with every other market."),
    Window("taper2013", "Taper tantrum, May-Aug 2013", date(2013, 5, 1), date(2013, 8, 31),
           "The US Fed signalled slower bond buying on 22-May-2013; the rupee and Indian bonds and equities fell."),
    Window("covid2020", "COVID-19 crash, Feb-Mar 2020", date(2020, 2, 1), date(2020, 3, 31),
           "The pandemic sell-off; the Nifty 50's low was on 23-Mar-2020."),
    Window("election2024", "Election-result day, 3-5 Jun 2024", date(2024, 6, 3), date(2024, 6, 5),
           "Exit-poll rally on 3-Jun, the result-day fall on 4-Jun-2024, a partial rebound on 5-Jun."),
    Window("febmar2026", "Feb-Mar 2026 fall", date(2026, 2, 1), date(2026, 3, 31),
           "The Feb-Mar 2026 correction (#397 cites a Nifty fall of about 14 %)."),
)  # fmt: skip


# --------------------------------------------------------------------------- paths
def market_days(level: dict[date, float], w: Window, until: date) -> tuple[date, list[date]] | str:
    """(base day, the market days after it up to `until`), or why the path cannot be used. The base is the last close
    before the window, at most MAX_GAP_DAYS before it; closes must follow each other within MAX_GAP_DAYS through the
    window's end (a backfill hole is never replayed as a flat market)."""
    ks = sorted(d for d, v in level.items() if v and v > 0)
    before = [d for d in ks if w.start - timedelta(days=MAX_GAP_DAYS) <= d < w.start]
    if not before:
        return f"no close in the {MAX_GAP_DAYS} days before {w.start:%d-%b-%Y}"
    base = before[-1]
    days = [d for d in ks if base < d <= until]
    inside = [base, *(d for d in days if d <= w.end)]
    after = until > w.end and not any(d > w.end for d in days)  # a close after the window proves none is missing
    if not days or after or w.end - inside[-1] > timedelta(days=MAX_GAP_DAYS):
        return f"closes stop before the window's end ({w.end:%d-%b-%Y})"
    for a, b in zip(inside, inside[1:], strict=False):
        if (b - a).days > MAX_GAP_DAYS:
            return f"no close between {a:%d-%b-%Y} and {b:%d-%b-%Y}: the stored history has a gap"
    return base, days


def _carried(closes: dict[date, float], ks: list[date], d: date) -> tuple[float, date] | None:
    """The last close on or before `d`, at most STALE_DAYS old (ks: the sorted close days)."""
    import bisect

    i = bisect.bisect_right(ks, d) - 1
    if i < 0 or (d - ks[i]).days > STALE_DAYS:
        return None
    return closes[ks[i]], ks[i]


def own_returns(closes: dict[date, float], factors: dict[date, float] | None, base: date, days: list[date],
                end: date) -> tuple[dict[date, float] | None, str | None]:  # fmt: skip
    """A holding's daily returns on the market days from its own closes (factors: split/bonus unit factors by
    ex-date), or (None, why). After the window the path ends where its closes end."""
    factors = factors or {}
    ks = sorted(d for d, v in closes.items() if v and v > 0)
    prev = _carried(closes, ks, base)
    if prev is None:
        return None, "no own price at the window's start"
    inwin = [d for d in days if d <= end]
    own_days = sum(1 for d in inwin if d in closes)
    if not inwin or own_days < MIN_COVERAGE * len(inwin):
        return None, f"own prices on {own_days} of the window's {len(inwin)} market days"
    out: dict[date, float] = {}
    for d in days:
        cur = _carried(closes, ks, d) if d <= end or d <= ks[-1] else None
        if cur is None:
            if d <= end:
                return None, f"no own price near {d:%d-%b-%Y} inside the window"
            break
        f = 1.0
        for x, k in factors.items():
            if prev[1] < x <= cur[1]:
                f *= k
        r = cur[0] * f / prev[0] - 1
        if abs(r) > JUMP and f == 1.0:
            return None, (f"a one-day move of {r:+.0%} on {d:%d-%b-%Y} with no recorded split or bonus "
                          "(probably an unrecorded split)")  # fmt: skip
        out[d] = r
        prev = cur
    return out, None
