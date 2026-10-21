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
from itertools import pairwise
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
    after = until > w.end and not any(
        d > w.end for d in days
    )  # a close after the window proves none is missing
    if not days or after or w.end - inside[-1] > timedelta(days=MAX_GAP_DAYS):
        return f"closes stop before the window's end ({w.end:%d-%b-%Y})"
    for a, b in pairwise(inside):
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


def level_returns(level: dict[date, float], base: date, days: list[date]) -> dict[date, float]:
    """Daily returns of the market path on its own days (from `market_days`, so every day has a close)."""
    out, prev = {}, level[base]
    for d in days:
        out[d] = level[d] / prev - 1
        prev = level[d]
    return out


def beta_returns(beta: float, market: dict[date, float]) -> dict[date, float]:
    return {d: beta * r for d, r in market.items()}


def duration_returns(mod_duration: float, yields: dict[date, float], base: date, days: list[date],
                     end: date) -> tuple[dict[date, float] | None, str | None]:  # fmt: skip
    """r_t = -D_mod × (y_t - y_(t-1)): the first-order price change of a bond for a parallel yield move (coupon carry
    and convexity left out; over weeks both are small next to the yield move). Yields are fractions."""
    ks = sorted(yields)
    prev = _carried(yields, ks, base)
    if prev is None:
        return None, "no yield at the window's start"
    inwin = [d for d in days if d <= end]
    have = sum(1 for d in inwin if d in yields)
    if not inwin or have < MIN_COVERAGE * len(inwin):
        return None, f"yields on {have} of the window's {len(inwin)} market days"
    out: dict[date, float] = {}
    for d in days:
        cur = _carried(yields, ks, d) if d <= end or d <= ks[-1] else None
        if cur is None:
            if d <= end:
                return None, f"no yield near {d:%d-%b-%Y} inside the window"
            break
        out[d] = -mod_duration * (cur[0] - prev[0])
        prev = cur
    return out, None


def estimate_beta(closes: dict[date, float], factors: dict[date, float] | None, market: dict[date, float],
                  market_factors: dict[date, float] | None, end: date, lookback_days: int = 365,
                  min_days: int = 120) -> tuple[float, int] | None:  # fmt: skip
    """(beta, days) of a holding against the market over the last `lookback_days` of common daily returns, or None
    when fewer than `min_days` (as portfolio.analytics.MIN_BETA) are available."""
    from finresearch.portfolio import analytics_math as am
    from finresearch.portfolio.history import returns_of

    since = end - timedelta(days=lookback_days)
    a = {d: r for d, r in returns_of(closes, factors).items() if since < d <= end}
    m = {d: r for d, r in returns_of(market, market_factors).items() if since < d <= end}
    common = sorted(set(a) & set(m))
    if len(common) < min_days:
        return None
    return am.beta([a[d] for d in common], [m[d] for d in common]), len(common)


# --------------------------------------------------------------------------- the replay
@dataclass
class Leg:
    key: str
    name: str
    value: float  # today's value, ₹
    how: str
    returns: dict[date, float]  # daily return on each market day the path covers


@dataclass
class Missing:
    key: str
    name: str
    value: float
    reason: str


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def _chain(returns: dict[date, float], days: list[date]) -> list[float]:
    lv, out = 1.0, [1.0]
    for d in days:
        lv *= 1 + returns[d]
        out.append(lv)
    return out


@dataclass
class Replay:
    window: Window
    base: date
    days: list[date]  # market days of the replay (after the base), cut where any leg's path ends
    market: dict[date, float]
    legs: list[Leg]
    missing: list[Missing]
    index_label: str
    notes: list[str] = field(default_factory=list)

    def levels(self) -> dict[str, list[float]]:
        """Each leg's level on [base, *days], base = 1."""
        return {g.key: _chain(g.returns, self.days) for g in self.legs}

    def values(self) -> list[float]:
        lv = self.levels()
        return [sum(g.value * lv[g.key][t] for g in self.legs) for t in range(len(self.days) + 1)]

    def payload(self) -> dict[str, Any]:
        from finresearch.portfolio import analytics_math as am

        w = self.window
        mapped = sum(g.value for g in self.legs)
        unmapped = sum(m.value for m in self.missing)
        out: dict[str, Any] = {
            "id": w.id, "label": w.label, "window": [w.start.isoformat(), w.end.isoformat()], "note": w.note,
            "base": self.base.isoformat(), "index_source": self.index_label, "notes": self.notes,
            "mapped_value": _r(mapped), "unmapped_value": _r(unmapped),
            "mapped_share": _r(mapped / (mapped + unmapped), 4) if mapped + unmapped > 0 else None,
            "unmapped": [{"key": m.key, "name": m.name, "value": _r(m.value), "reason": m.reason}
                         for m in sorted(self.missing, key=lambda m: -m.value)],
        }  # fmt: skip
        n_in = sum(1 for d in self.days if d <= w.end)
        mk = _chain(self.market, self.days)
        mdd = am.drawdown_recovery(mk[: n_in + 1])
        out["index"] = {"move": _r(mk[n_in] - 1, 4), "drawdown": _r(mdd.depth, 4)}
        if mapped <= 0:
            return {**out, "available": False, "reason": "no holding could be mapped to a price path for this window",
                    "loss": None, "worst_day": None, "drawdown": None, "recovery": None, "contributors": [],
                    "positions": []}  # fmt: skip
        v, lv = self.values(), self.levels()
        dd = am.drawdown_recovery(v[: n_in + 1])
        daily = [(self.days[t - 1], v[t] / v[t - 1] - 1, v[t] - v[t - 1]) for t in range(1, n_in + 1)]
        worst = min(daily, key=lambda x: x[1])
        day = [self.base, *self.days]
        rec = (
            next((t for t in range(dd.trough + 1, len(v)) if v[t] >= v[dd.peak]), None)
            if dd.depth < 0
            else None
        )
        last = day[-1]
        if dd.depth >= 0:
            note = "the replayed value never fell below its start inside the window"
        elif rec is None:
            note = f"not back at the {day[dd.peak]:%d-%b-%Y} peak by {last:%d-%b-%Y} (the end of the data searched)"
        else:
            note = f"back at the {day[dd.peak]:%d-%b-%Y} peak on {day[rec]:%d-%b-%Y}"
        contrib = [{"key": g.key, "name": g.name, "inr": _r(g.value * (lv[g.key][dd.trough] - lv[g.key][dd.peak]))}
                   for g in self.legs]  # fmt: skip
        contrib.sort(key=lambda x: x["inr"] or 0)
        fall = v[dd.trough] - v[dd.peak]
        for c in contrib:
            c["share"] = _r((c["inr"] or 0) / fall, 4) if fall < 0 else None
        out.update(
            available=True,
            loss={"inr": _r(v[n_in] - mapped), "pct": _r(v[n_in] / mapped - 1, 4), "day": day[n_in].isoformat()},
            worst_day={"date": worst[0].isoformat(), "pct": _r(worst[1], 4), "inr": _r(worst[2])},
            drawdown={"peak": day[dd.peak].isoformat(), "trough": day[dd.trough].isoformat(), "pct": _r(dd.depth, 4),
                      "inr": _r(fall)},
            recovery={"date": day[rec].isoformat() if rec is not None else None,
                      "days": (day[rec] - day[dd.trough]).days if rec is not None else None,
                      "searched_to": last.isoformat(), "note": note},
            contributors=contrib[:TOP],
            positions=sorted([{"key": g.key, "name": g.name, "value": _r(g.value), "how": g.how,
                               "move": _r(lv[g.key][n_in] - 1, 4),
                               "drawdown_inr": _r(g.value * (lv[g.key][dd.trough] - lv[g.key][dd.peak]))}
                              for g in self.legs], key=lambda x: x["drawdown_inr"] or 0),
            path=[{"date": d.isoformat(), "value": _r(x), "index": _r(m * 100, 3)}
                  for d, x, m in zip(day, v, mk, strict=True)],
        )  # fmt: skip
        return out


def replay(w: Window, base: date, days: list[date], market: dict[date, float], legs: list[Leg],
           missing: list[Missing], index_label: str) -> Replay:  # fmt: skip
    """Today's mapped holdings replayed over the market days. The replay ends on the last day every path covers (a
    leg's own prices may end after the window); a leg missing a day inside the window is moved to `missing`."""
    inwin = [d for d in days if d <= w.end]
    keep, gone = [], list(missing)
    for g in legs:
        if all(d in g.returns for d in inwin):
            keep.append(g)
        else:
            gone.append(Missing(g.key, g.name, g.value, f"{g.how}: the path misses days inside the window"))
    cut = len(days)
    for g in keep:
        cut = min(cut, next((i for i, d in enumerate(days) if d not in g.returns), len(days)))
    days = days[:cut]
    return Replay(w, base, days, {d: market[d] for d in days}, keep, gone, index_label)


# --------------------------------------------------------------------------- mapping one holding
# AMFI scheme category (SEBI's Oct-2017 categorisation, the AMFI NAV file's heading) -> the index_eod symbols that
# stand for it, newest name first: indices launched later (Midcap 150, Smallcap 250 and LargeMidcap 250 in 2016-17) or
# renamed in 2015 (CNX -> NIFTY) are tried in turn. [unverified: the older names are taken from NSE's 2015 rebrand and
# not checked against stored rows; a name with no rows for the window simply does not map, it never reads as 0 %]
CATEGORY_INDEX: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("large & mid cap",), ("NIFTY LARGEMIDCAP 250", "NIFTY 500", "CNX 500")),
    (("large cap",), ("NIFTY 100", "CNX 100")),
    (("mid cap",), ("NIFTY MIDCAP 150", "NIFTY MIDCAP 100", "CNX MIDCAP")),
    (("small cap",), ("NIFTY SMALLCAP 250", "NIFTY SMALLCAP 100", "CNX SMALLCAP")),
    (
        ("flexi cap", "multi cap", "elss", "focused", "value fund", "contra", "dividend yield"),
        ("NIFTY 500", "CNX 500"),
    ),
)


def category_indices(category: str | None) -> tuple[str, ...]:
    c = (category or "").lower()
    if not c.startswith("equity"):
        return ()
    for words, syms in CATEGORY_INDEX:
        if any(w in c for w in words):
            return syms
    return ()


@dataclass
class HoldingData:
    key: str
    name: str
    value: float
    asset_type: str
    tax_class: str
    closes: dict[date, float]  # own closes over the replay's range
    factors: dict[date, float]  # split/bonus unit factors by ex-date
    recent: dict[date, float]  # the last year's closes (for betas)
    category: str | None = None  # AMFI scheme category of a fund
    mod_duration: float | None = None  # a bond's modified duration, when its terms and yield give one


@dataclass
class Context:
    base: date
    days: list[date]
    end: date  # the window's end
    market: dict[date, float]  # the market path's daily returns
    market_label: str
    market_recent: dict[date, float]  # NIFTYBEES closes over the last year (betas)
    market_factors: dict[date, float] | None
    as_of: date  # the end of the beta lookback
    categories: dict[
        str, tuple[dict[date, float] | None, dict[date, float]]
    ]  # symbol -> (window path, recent)
    yields: dict[date, float] | None  # a G-sec yield series for the window, where one exists


def map_holding(x: HoldingData, c: Context) -> Leg | Missing:
    """The first path that has data (module docstring), or Missing with every reason tried."""
    own, why = own_returns(x.closes, x.factors, c.base, c.days, c.end)
    if own is not None:
        return Leg(x.key, x.name, x.value, "own NAVs" if x.asset_type == "mf" else "own prices", own)
    reasons = [why or "no own prices"]
    if x.asset_type == "mf":
        for sym in category_indices(x.category):
            path, recent = c.categories.get(sym, (None, {}))
            if path is None:
                continue
            b = estimate_beta(x.recent, x.factors, recent, None, c.as_of)
            if b is not None:
                how = f"beta {b[0]:.2f} × {sym} (category index; beta from {b[1]} days)"
                return Leg(x.key, x.name, x.value, how, beta_returns(b[0], path))
        if category_indices(x.category):
            reasons.append("no category index for this window")
    if x.tax_class == "equity":
        b = estimate_beta(x.recent, x.factors, c.market_recent, c.market_factors, c.as_of)
        if b is not None:
            how = f"beta {b[0]:.2f} × {c.market_label} (beta to NIFTYBEES from {b[1]} days)"
            return Leg(x.key, x.name, x.value, how, beta_returns(b[0], c.market))
        reasons.append("under 120 days of recent prices to estimate a beta")
    elif x.mod_duration is not None:
        if c.yields is None:
            reasons.append("no stored yield history for this window (duration × yield change)")
        else:
            r, ywhy = duration_returns(x.mod_duration, c.yields, c.base, c.days, c.end)
            if r is not None:
                return Leg(
                    x.key, x.name, x.value, f"-{x.mod_duration:.2f} modified duration × yield change", r
                )
            reasons.append(ywhy or "no yields")
    else:
        reasons.append("not equity, so no beta mapping")
    return Missing(x.key, x.name, x.value, "; ".join(reasons))
