"""Mutual-fund signal: invest, keep a SIP going, hold, or review a switch (docs/dev/RESEARCH_ROADMAP.md §D.3).

Why it looks like this. SPIVA India finds most active funds trail their benchmark over 5 and 10 years (large-cap
90 % / 73 %, mid/small-cap 67 % / 82 %, ELSS 68 % / 87 %, year-end 2024 via secondary coverage [47][48]), and
performance persistence is weak: what persists is mostly cost, and the persistent *under*performance of the worst
funds (Carhart 1997 [49]). So this signal does not try to pick winners. It checks:

* cost: the direct plan's total expense ratio (TER) against same-category peers from AMFI's daily TER file, and
  whether this is the regular plan (which pays a distributor commission every year);
* consistency: in how many quarter-end windows the fund's 1- and 3-year returns beat the median of its SEBI category
  (direct-growth peers compared with direct-growth, regular with regular), from AMFI NAVs;
* downside: its capture of the category's falling quarters and its quarter-end drawdown against the category's;
* style sanity: how closely its quarterly returns move with the category's (R²), a returns-based stand-in for a
  holdings check (holdings files are not read yet);
* fit: whether the category suits the investor's horizon and risk appetite (profile, or `horizon` / `risk` in ctx).

The probability is a historical base rate: the share of past 3-year windows in which the fund's return was at or
above its category median, with a Wilson interval on the *effective* number of windows (overlapping windows are not
independent). Past consistency is a weak guide to the next three years, so the interval is wide on purpose.

Actions follow the roadmap's rules, not the score: BUY when the category fits, the TER is in the category's cheaper
half, the 3-year hit rate is at least 60 % and nothing flags style drift (direct plan); REDUCE ("switch review") when
the fund has trailed its category median in most windows *and* is expensive or a regular plan; AVOID when the
category doesn't fit; ACCUMULATE (keep or start a SIP) when it fits and beats the median at least half the time;
HOLD otherwise; NO_SIGNAL when the history or peer data is too thin, or for index funds and ETFs (the category-median
test doesn't apply to a passive fund).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from finresearch.api.markets import TtlCache
from finresearch.fincalc import funds as F
from finresearch.fincalc.dates import add_years, today_ist
from finresearch.signals.base import Factor, Signal, Validation, clip_score
from finresearch.signals.registry import register

NAV_ALL_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
TER_PAGE = "https://www.amfiindia.com/ter-of-mf-schemes"
SPIVA_URL = (
    "https://cafemutual.com/news/industry/35917-73-of-indian-large-cap-and-82-of-mid-and-small-cap-funds-have-"
    "underperformed-sp-india-benchmarks-over-a-10-year-period"
)
CARHART_URL = "https://doi.org/10.1111/j.1540-6261.1997.tb03808.x"
SEBI_MF_CIRCULAR = (
    "https://www.sebi.gov.in/legal/master-circulars/mar-2026/master-circular-for-mutual-funds_100491.html"
)

HISTORY_YEARS = 7  # quarter-end snapshots this far back: 3-year windows ending over the last 4 years
MIN_PEERS = 5
MIN_WINDOWS_3Y = 4
QUARTER_DAYS = 91.3
INVEST_HIT_RATE = 0.60  # roadmap §D.3: rolling 3-year return above the category median in >= 60 % of windows
UNDERPERFORM_HIT_RATE = 0.40
STYLE_R2_FLAG = 0.60  # below this the fund's quarterly returns barely move with its category's [W]
MIN_QUARTERS_R2 = 12
MIN_DOWN_QUARTERS = 3

Snapshot = dict[str, tuple[date, Decimal]]  # scheme code -> (NAV date, NAV) on or before an anchor


# --------------------------------------------------------------------------- data sources (overridable in tests)
@dataclass
class FundSources:
    schemes: Callable[[], Awaitable[list]] | None = None  # () -> AMFI NAVAll rows (SchemeNav)
    snapshot: Callable[[date], Awaitable[Snapshot]] | None = (
        None  # anchor -> every scheme's NAV on or before it
    )
    ter: Callable[[date], Awaitable[dict]] | None = None  # month -> {ter_key(name): SchemeTer}
    profile: Callable[[], Awaitable[Any]] | None = None  # () -> suggest.profile.Profile
    today: Callable[[], date] = today_ist


SOURCES = FundSources()


async def _schemes() -> list:
    if SOURCES.schemes is not None:
        return await SOURCES.schemes()
    from finresearch.adapters.amfi import AmfiClient
    from finresearch.mcp_server.server import _nav_all

    async with AmfiClient() as amfi:
        return await _nav_all(amfi)


def _snapshot_path(anchor: date):
    from finresearch.config import get_settings

    return get_settings().state_dir / "amfi_snapshots" / f"{anchor.isoformat()}.json"


async def _snapshot(anchor: date) -> Snapshot:
    """Every scheme's latest NAV on or before `anchor` (one all-AMC AMFI history request, a wider one if the day
    was a holiday). Past snapshots never change, so they are kept on disk as compact JSON."""
    if SOURCES.snapshot is not None:
        return await SOURCES.snapshot(anchor)
    path = _snapshot_path(anchor)
    if path.exists():
        raw = json.loads(path.read_text())
        return {c: (date.fromisoformat(d), Decimal(v)) for c, (d, v) in raw["navs"].items()}
    from finresearch.adapters.amfi import AmfiClient

    async with AmfiClient() as amfi:
        rows = await amfi.history(anchor, anchor)
        if len(rows) < 2000:  # a holiday: only a few debt schemes publish
            rows = await amfi.history(anchor - timedelta(days=6), anchor)
    out: Snapshot = {}
    for r in rows:
        if r.nav and r.day and r.day <= anchor and (r.code not in out or r.day > out[r.code][0]):
            out[r.code] = (r.day, r.nav)
    if out and anchor < SOURCES.today() - timedelta(days=5):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"anchor": anchor.isoformat(), "source": HISTORY_URL,
                                    "navs": {c: [d.isoformat(), str(v)] for c, (d, v) in out.items()}}))  # fmt: skip
    return out


async def _ter(month: date) -> dict:
    if SOURCES.ter is not None:
        return await SOURCES.ter(month)
    from finresearch.adapters.amfi import AmfiClient

    async with AmfiClient() as amfi:
        try:
            return await amfi.ter(month)
        except Exception:
            prev = month.replace(day=1) - timedelta(days=1)  # early in a month the new file can be empty
            return await amfi.ter(prev)


async def _profile():
    if SOURCES.profile is not None:
        return await SOURCES.profile()

    def load():
        from finresearch.db import session_scope
        from finresearch.suggest.advisor import load_profile
        from finresearch.suggest.profile import default_profile

        try:
            with session_scope() as s:
                return load_profile(s)
        except Exception:
            return default_profile()

    return await asyncio.to_thread(load)


# --------------------------------------------------------------------------- pure helpers
def quarter_ends(start: date, end: date) -> list[date]:
    """Calendar quarter ends (31 Mar, 30 Jun, 30 Sep, 31 Dec) in [start, end], each moved back to a weekday."""
    out, y = [], start.year
    while y <= end.year:
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            q = date(y, m, d)
            while q.weekday() >= 5:
                q -= timedelta(days=1)
            if start <= q <= end:
                out.append(q)
        y += 1
    return out


def is_growth(x) -> bool:
    text = f"{x.option or ''} {x.name}".lower()
    return "growth" in text and "idcw" not in text and "dividend" not in text


def is_direct(x) -> bool:
    return "direct" in (x.plan or x.name).lower()


def is_passive(category: str | None, name: str = "") -> bool:
    c, n = (category or "").lower(), name.lower()
    return "index fund" in c or "exchange traded" in c or "etf" in c or " etf" in n or "index fund" in n


RISK_RANK = {"low": 0, "medium": 1, "high": 2}
HORIZON_RANK = {"short": 0, "medium": 1, "long": 2}


def category_needs(category: str | None, name: str = "") -> tuple[str, str] | None:
    """(minimum horizon, minimum risk appetite) a SEBI category calls for: equity needs a long horizon (5+ years),
    mid/small-cap and sectoral funds a high risk appetite, liquid and short-duration debt suit a short horizon.
    A judgment table [W] built on SEBI's category definitions and riskometer levels (Master Circular for MFs)."""
    c = f"{category or ''} {name}".lower()
    table = [
        (("overnight", "liquid", "money market", "ultra short", "low duration", "arbitrage"), ("short", "low")),
        (("short duration", "banking and psu", "banking & psu", "corporate bond", "floater", "conservative hybrid"),
         ("short", "low")),
        (("credit risk",), ("medium", "medium")),
        (("medium duration", "medium to long", "long duration", "dynamic bond", "gilt"), ("medium", "medium")),
        (("equity savings", "balanced advantage", "dynamic asset allocation", "multi asset"), ("medium", "medium")),
        (("small cap", "smallcap", "mid cap", "midcap", "sectoral", "thematic", "overseas", "international"),
         ("long", "high")),
        (("aggressive hybrid", "large cap", "large & mid", "flexi cap", "multi cap", "value", "contra",
          "dividend yield", "focused", "elss", "nifty", "sensex", "equity", "retirement", "children"),
         ("long", "medium")),
    ]  # fmt: skip
    for words, needs in table:
        if any(w in c for w in words):
            return needs
    return None


INDEX_FOR_CATEGORY = [  # the index an index-fund alternative would track, by category keyword
    ("large & mid", ("nifty largemidcap 250",)),
    ("large cap", ("nifty 50 index", "nifty 100 index", "sensex index")),
    ("mid cap", ("nifty midcap 150",)),
    ("small cap", ("nifty smallcap 250",)),
    ("multi cap", ("multicap 50:25:25",)),
    ("flexi cap", ("nifty 500 index",)),
    ("elss", ("elss tax saver nifty", "elss nifty")),
    ("value", ("nifty 500 value 50", "nifty50 value 20")),
]


# --------------------------------------------------------------------------- analysis
@dataclass
class Window:
    end: date
    years: int
    fund: float
    median: float
    p25: float
    p75: float
    percentile: float
    peers: int

    def json(self) -> dict[str, Any]:
        return {"end": self.end.isoformat(), "years": self.years, "fund": round(self.fund, 6),
                "median": round(self.median, 6), "p25": round(self.p25, 6), "p75": round(self.p75, 6),
                "percentile": round(self.percentile, 4), "peers": self.peers}  # fmt: skip


def _quantile(vals: list[float], q: float) -> float:
    s = sorted(vals)
    pos = (len(s) - 1) * q
    i, frac = int(pos), pos - int(pos)
    return s[i] + (s[min(i + 1, len(s) - 1)] - s[i]) * frac


def _nav_near(snap: Snapshot, code: str, anchor: date) -> tuple[date, Decimal] | None:
    hit = snap.get(code)
    return (
        hit if hit and (anchor - hit[0]).days <= 10 else None
    )  # a stale NAV means the scheme wasn't priced then


def windows(me_code: str, peer_codes: list[str], anchors: list[date], snaps: list[Snapshot],
            years: int) -> list[Window]:  # fmt: skip
    """The fund's annualised `years`-year return against its peers' for every pair of anchors `years` apart."""
    step = 4 * years
    out = []
    for i in range(len(anchors) - step):
        a0, a1 = anchors[i], anchors[i + step]
        s0, s1 = snaps[i], snaps[i + step]

        def ret(code: str, s0=s0, s1=s1, a0=a0, a1=a1) -> float | None:
            p0, p1 = _nav_near(s0, code, a0), _nav_near(s1, code, a1)
            if not p0 or not p1 or p1[0] <= p0[0]:
                return None
            return float(F.annualised_return(p0[1], p1[1], p0[0], p1[0]))

        mine = ret(me_code)
        peers = [r for r in (ret(c) for c in peer_codes) if r is not None]
        if mine is None or len(peers) < MIN_PEERS:
            continue
        out.append(Window(a1, years, mine, F.median(peers), _quantile(peers, 0.25), _quantile(peers, 0.75),
                          F.percentile_rank(peers, mine), len(peers)))  # fmt: skip
    return out


def quarterly(
    me_code: str, peer_codes: list[str], anchors: list[date], snaps: list[Snapshot]
) -> dict[str, Any]:
    """Quarter-on-quarter returns of the fund and of the category median; downside capture, R², and quarter-end
    drawdowns of the fund against the median peer."""
    fund_q, med_q = [], []
    for i in range(1, len(anchors)):

        def qret(code: str, i=i) -> float | None:
            p0, p1 = _nav_near(snaps[i - 1], code, anchors[i - 1]), _nav_near(snaps[i], code, anchors[i])
            return float(p1[1] / p0[1] - 1) if p0 and p1 and p1[0] > p0[0] else None

        mine = qret(me_code)
        peers = [r for r in (qret(c) for c in peer_codes) if r is not None]
        if mine is not None and len(peers) >= MIN_PEERS:
            fund_q.append(mine)
            med_q.append(F.median(peers))
    down = sum(1 for m in med_q if m < 0)
    capture = F.downside_capture(fund_q, med_q) if down >= MIN_DOWN_QUARTERS else None
    r2 = F.r_squared(med_q, fund_q) if len(fund_q) >= MIN_QUARTERS_R2 else None

    def drawdown(code: str) -> float | None:
        navs = [p[1] for a, s in zip(anchors, snaps, strict=True) if (p := _nav_near(s, code, a))]
        if len(navs) < max(8, len(anchors) // 2):
            return None
        from finresearch.fincalc.market import max_drawdown

        return float(max_drawdown(navs).max_drawdown)

    mine_dd = drawdown(me_code)
    peer_dd = [d for d in (drawdown(c) for c in peer_codes) if d is not None]
    return {"quarters": len(fund_q), "down_quarters": down, "downside_capture": capture, "r_squared": r2,
            "drawdown": mine_dd, "category_drawdown": F.median(peer_dd) if len(peer_dd) >= MIN_PEERS else None,
            "fund_q": fund_q, "median_q": med_q}  # fmt: skip


def ter_stats(me, peers: list, table: dict) -> dict[str, Any]:
    """The fund's TER for its own plan, its percentile among same-category peers of the same plan type (0 =
    cheapest), and the regular-minus-direct gap of its own scheme."""
    from finresearch.adapters.amfi import ter_key

    if not table:
        return {"available": False}
    mine = table.get(ter_key(me.name))
    if mine is None:
        return {"available": True, "matched": False}
    direct = is_direct(me)
    own = mine.direct if direct else mine.regular
    peer_ters = []
    for p in peers:
        t = table.get(ter_key(p.name))
        v = (t.direct if direct else t.regular) if t else None
        if v is not None:
            peer_ters.append(float(v))
    pct = (sum(1 for v in peer_ters if v < float(own)) + 0.5 * sum(1 for v in peer_ters if v == float(own))) / len(
        peer_ters) if own is not None and peer_ters else None  # fmt: skip
    return {"available": True, "matched": True, "ter": float(own) if own is not None else None,
            "direct_ter": float(mine.direct) if mine.direct is not None else None,
            "regular_ter": float(mine.regular) if mine.regular is not None else None, "day": mine.day.isoformat(),
            "percentile": pct, "peers": len(peer_ters),
            "category_median": F.median(peer_ters) if peer_ters else None}  # fmt: skip


def index_alternatives(category: str | None, rows: list, table: dict, limit: int = 3) -> list[dict[str, Any]]:
    """The cheapest direct-growth index funds tracking the index that matches the category (e.g. Nifty Midcap 150
    for a mid-cap fund), by direct-plan TER."""
    from finresearch.adapters.amfi import ter_key

    c = (category or "").lower()
    keys = next((k for word, k in INDEX_FOR_CATEGORY if word in c), None)
    if not keys:
        return []
    hits = []
    for x in rows:
        if not (
            x.is_direct_growth and is_passive(x.category, x.name) and any(k in x.name.lower() for k in keys)
        ):
            continue
        t = table.get(ter_key(x.name)) if table else None
        hits.append({"scheme_code": x.code, "name": x.name,
                     "direct_ter": float(t.direct) if t and t.direct is not None else None})  # fmt: skip
    hits.sort(key=lambda h: (h["direct_ter"] is None, h["direct_ter"] or 0, h["name"]))
    return hits[:limit]


async def analyse(code: str) -> dict[str, Any]:
    """Everything the fund signal and its chart need, from AMFI (cached per scheme for 12 hours)."""
    return await _CACHE.get(("fund", code), 12 * 3600, lambda: _analyse(code))


async def _analyse(code: str) -> dict[str, Any]:
    code = code.strip()
    if not code.isdigit() or len(code) > 8:
        raise ValueError(f"{code!r} is not an AMFI scheme code")
    rows = await _schemes()
    me = next((x for x in rows if x.code == code), None)
    if me is None:
        raise LookupError(f"scheme {code} is not in AMFI's NAV file")
    direct = is_direct(me)
    peers = [x for x in rows if x.category == me.category and x.code != me.code and x.name != me.name
             and is_growth(x) and is_direct(x) == direct and x.nav and x.day]  # fmt: skip
    last = me.day or SOURCES.today()
    anchors = quarter_ends(add_years(last, -HISTORY_YEARS), min(last, SOURCES.today() - timedelta(days=1)))
    passive = is_passive(me.category, me.name)
    snaps: list[Snapshot] = []
    if not passive:
        for a in anchors:
            snaps.append(await _snapshot(a))
    codes = [p.code for p in peers]
    w3 = windows(me.code, codes, anchors, snaps, 3) if snaps else []
    w1 = windows(me.code, codes, anchors, snaps, 1) if snaps else []
    q = quarterly(me.code, codes, anchors, snaps) if snaps else {}
    ter_errors = None
    unreachable: list[str] = []
    try:
        table = await _ter(SOURCES.today())
    except Exception as e:  # the TER file is optional: the factor is marked missing
        from finresearch.adapters.http import is_transient

        table, ter_errors = {}, f"AMFI TER file unavailable: {e}"
        if is_transient(e):  # AMFI unreachable just now: keep this answer for seconds, not 12 hours
            unreachable.append("AMFI TER file")
    return {"unreachable": unreachable, "scheme": {"scheme_code": me.code, "name": me.name, "plan": me.plan, "option": me.option,
                       "category": me.category, "amc": me.amc, "nav_date": me.day.isoformat() if me.day else None},
            "direct": direct, "growth": is_growth(me), "passive": passive, "peers": len(peers),
            "anchors": [a.isoformat() for a in anchors],
            "windows_3y": [w.json() for w in w3], "windows_1y": [w.json() for w in w1],
            "quarterly": {k: v for k, v in q.items() if k not in ("fund_q", "median_q")},
            "ter": ter_stats(me, peers, table), "ter_error": ter_errors,
            "index_alternatives": index_alternatives(me.category, rows, table),
            "as_of": datetime.now(UTC).isoformat(),
            "sources": [NAV_ALL_URL, HISTORY_URL, TER_PAGE]}  # fmt: skip


_CACHE = TtlCache()


# --------------------------------------------------------------------------- the signal
def hit_rate(ws: list[dict[str, Any]]) -> tuple[int, int]:
    """(windows at or above the category median, windows)."""
    return sum(1 for w in ws if w["fund"] >= w["median"]), len(ws)


def _investor(profile, ctx: dict[str, Any]) -> tuple[str | None, str, str]:
    """(horizon rank name, risk appetite, where they came from). ctx `horizon` (short|medium|long) or
    `horizon_years`, and `risk` (low|medium|high), override the profile. The profile's "listing" horizon is about
    flipping IPOs and says nothing about a fund holding period, so it leaves the horizon unknown (None)."""
    src = "profile"
    horizon = {"short": "short", "long": "long"}.get(getattr(profile, "horizon", None) or "")
    risk = getattr(profile, "risk_appetite", "medium")
    if ctx.get("horizon") in HORIZON_RANK:
        horizon, src = ctx["horizon"], "chosen"
    elif ctx.get("horizon_years"):
        try:
            yrs = float(ctx["horizon_years"])
        except ValueError as e:
            raise ValueError("horizon_years must be a number") from e
        horizon, src = ("short" if yrs < 3 else "medium" if yrs < 5 else "long"), "chosen"
    if ctx.get("risk") in RISK_RANK:
        risk, src = ctx["risk"], "chosen"
    return horizon, risk, src


def build_signal(a: dict[str, Any], profile, ctx: dict[str, Any] | None = None) -> Signal:
    """The fund signal from an `analyse` result: factors, the §D.3 action rules, and the base-rate probability."""
    ctx = ctx or {}
    s = a["scheme"]
    factors: list[Factor] = []
    caveats: list[str] = []
    reasons_missing: list[str] = []
    event = "3-year return at or above the category median"
    as_of = datetime.fromisoformat(a["as_of"]) if a.get("as_of") else datetime.now(UTC)
    common = dict(asset="fund", instrument=s["scheme_code"], name=s["name"], horizon="3 years",
                  sources=[*a.get("sources", []), SPIVA_URL, CARHART_URL, SEBI_MF_CIRCULAR], as_of=as_of)  # fmt: skip

    # --- cost
    ter = a.get("ter", {})
    if not a["direct"]:
        gap = (ter.get("regular_ter") or 0) - (ter.get("direct_ter") or 0) if ter.get("matched") else None
        drag = None
        if gap and gap > 0:
            drag = float(F.expense_drag(100000, 10, "0.12", Decimal(str(gap)) / 100, 0))
        factors.append(Factor("Regular plan", round(gap, 2) if gap is not None else "regular", -15.0,
                              "A regular plan pays a distributor commission out of the fund every year; the direct plan "
                              "of the same scheme holds the same portfolio for less"
                              + (f" ({gap:.2f} pp a year here: on ₹1 lakh growing 12 % a year that is about "
                                 f"₹{drag:,.0f} less after 10 years)." if drag else "."),
                              source=TER_PAGE, unit="pp TER gap" if gap is not None else None))  # fmt: skip
        caveats.append("This is the regular plan: the direct plan of the same scheme is cheaper. Before switching, "
                       "check the exit load and the capital-gains tax on units you'd redeem.")  # fmt: skip
    else:
        factors.append(Factor("Direct plan", "direct", 5.0, "No distributor commission: the cheapest way to hold "
                              "this scheme.", source=TER_PAGE))  # fmt: skip
    ter_pct = ter.get("percentile")
    if ter.get("matched") and ter.get("ter") is not None and ter_pct is not None:
        contrib = round(20 * (0.5 - ter_pct), 1)
        factors.append(Factor("Expense ratio vs category", ter["ter"], contrib,
                              f"Total expense ratio {ter['ter']:.2f} % a year on {ter['day']}; cheaper than "
                              f"{(1 - ter_pct) * 100:.0f} % of {ter['peers']} same-plan peers (category median "
                              f"{ter['category_median']:.2f} %). Cost is the one thing that reliably persists.",
                              source=TER_PAGE, unit="% a year"))  # fmt: skip
    else:
        why = a.get("ter_error") or ("AMFI's TER file has no row with this scheme's name" if ter.get("available")
                                     else "the TER file was not read")  # fmt: skip
        factors.append(Factor("Expense ratio vs category", None, 0.0, f"Missing: {why}.", source=TER_PAGE))
        reasons_missing.append("expense ratio")

    # --- fit
    horizon, risk, src = _investor(profile, ctx)
    needs = category_needs(s["category"], s["name"])
    fits: bool | None = None
    if needs and horizon is None:
        factors.append(Factor("Fits your horizon and risk", None, 0.0,
                              f"{s['category']} calls for at least a {needs[0]} horizon and {needs[1]} risk appetite. "
                              "Your profile's horizon is set for IPO listings, so the fit isn't judged: set a short or "
                              "long horizon in the profile to include it.", source="suggest.profile"))  # fmt: skip
        caveats.append(
            f"Fit not checked: this category calls for a {needs[0]} horizon and {needs[1]} risk appetite."
        )
    elif needs:
        fits = HORIZON_RANK[horizon] >= HORIZON_RANK[needs[0]] and RISK_RANK[risk] >= RISK_RANK[needs[1]]
        factors.append(Factor("Fits your horizon and risk", "yes" if fits else "no", 10.0 if fits else -40.0,
                              f"{s['category']} calls for at least a {needs[0]} horizon and {needs[1]} risk appetite; "
                              f"yours ({src}): {horizon} horizon, {risk} risk.",
                              source="suggest.profile"))  # fmt: skip
    else:
        factors.append(Factor("Fits your horizon and risk", None, 0.0,
                              f"No suitability rule for the category {s['category']!r}; judge the fit yourself."))  # fmt: skip

    def signal(
        action: str, probability=None, interval=None, validation=None, base_rate=None, method=""
    ) -> Signal:
        score = clip_score(sum(f.contribution for f in factors))
        return Signal(action=action, score=round(score, 1), event=event,
                      method=method or "§D.3 rules on AMFI NAV, TER and category data", probability=probability,
                      probability_interval=interval, base_rate=base_rate, factors=factors, caveats=caveats,
                      validation=validation or Validation(status="rule_based",
                                                          description="Fixed rules from published research; not yet "
                                                                      "checked against this app's own outcomes."),
                      **common)  # fmt: skip

    if a.get("passive"):
        caveats.append("Index fund or ETF: judge it by cost and tracking error against other funds on the same index; "
                       "beating the category median is not what it is for.")  # fmt: skip
        return signal("NO_SIGNAL", method="not applied to passive funds")
    if not a.get("growth"):
        caveats.append("IDCW option: payouts leave the NAV, so its returns understate the fund's. The growth option of "
                       "the same scheme gives the fair comparison.")  # fmt: skip

    w3, w1 = a.get("windows_3y", []), a.get("windows_1y", [])
    k3, n3 = hit_rate(w3)
    if n3 < MIN_WINDOWS_3Y or a.get("peers", 0) < MIN_PEERS:
        caveats.append(f"Not enough history to judge consistency: {n3} quarter-end 3-year windows with at least "
                       f"{MIN_PEERS} category peers (need {MIN_WINDOWS_3Y}).")  # fmt: skip
        return signal("NO_SIGNAL", method="insufficient history")

    hit3 = k3 / n3
    n_eff3 = F.effective_windows(n3, QUARTER_DAYS, 3 * 365.25)
    ci = F.wilson_interval(hit3 * n_eff3, n_eff3)
    last3 = w3[-1]
    factors.append(Factor("3-year returns vs category median", round(hit3 * 100, 1), round(60 * (hit3 - 0.5), 1),
                          f"At or above the median of {last3['peers']} same-plan category peers in {k3} of {n3} "
                          f"quarter-end 3-year windows. Latest (to {last3['end']}): {last3['fund'] * 100:.1f} % a year "
                          f"vs median {last3['median'] * 100:.1f} %.", source=HISTORY_URL, unit="% of windows"))  # fmt: skip
    k1, n1 = hit_rate(w1)
    if n1:
        factors.append(Factor("1-year returns vs category median", round(k1 / n1 * 100, 1),
                               round(20 * (k1 / n1 - 0.5), 1),
                               f"At or above the category median in {k1} of {n1} quarter-end 1-year windows.",
                               source=HISTORY_URL, unit="% of windows"))  # fmt: skip
    q = a.get("quarterly", {})
    cap = q.get("downside_capture")
    if cap is not None:
        contrib = 5.0 if cap < 0.9 else -5.0 if cap > 1.1 else 0.0
        factors.append(Factor("Downside capture vs category", round(cap * 100, 1), contrib,
                              f"In the {q['down_quarters']} quarters the category median fell, the fund fell "
                              f"{cap * 100:.0f} % as much (below 100 % = it cushioned falls).", source=HISTORY_URL,
                              unit="%"))  # fmt: skip
    if q.get("drawdown") is not None and q.get("category_drawdown") is not None:
        diff = q["drawdown"] - q["category_drawdown"]  # negative: the fund fell further
        contrib = 5.0 if diff > 0.02 else -5.0 if diff < -0.02 else 0.0
        factors.append(Factor("Max drawdown vs category", round(q["drawdown"] * 100, 1), contrib,
                              f"Worst fall between quarter-end NAVs: {q['drawdown'] * 100:.1f} % against the category "
                              f"median's {q['category_drawdown'] * 100:.1f} % (quarter-end sampling misses falls "
                              f"that recovered within a quarter).", source=HISTORY_URL, unit="%"))  # fmt: skip
    r2 = q.get("r_squared")
    drift = r2 is not None and r2 < STYLE_R2_FLAG
    if r2 is not None:
        factors.append(Factor("Moves with its category (R²)", round(r2, 2), -10.0 if drift else 0.0,
                              f"{r2 * 100:.0f} % of the fund's quarterly return swings moved with the category median "
                              f"over {q['quarters']} quarters. Low values can mean style drift: a mid-cap fund "
                              f"behaving like a small-cap one. A returns-based check, not a holdings audit.",
                              source=HISTORY_URL))  # fmt: skip
    caveats.append(f"The fund beat its category median in {k3} of {n3} past 3-year windows ({hit3 * 100:.0f} %, the "
                   f"base rate). Overlapping windows are worth only about {n_eff3:.1f} independent ones, so the "
                   f"probability shown is that hit rate pulled towards 50 % (the centre of its Wilson interval) and "
                   f"the range is wide. Persistence is weak (Carhart 1997; SPIVA India): past consistency guides only "
                   f"a little.")  # fmt: skip
    caveats.append("Peers are today's schemes in the category: merged or closed funds are missing (survivorship "
                   "bias flatters the median's survivors).")  # fmt: skip

    # --- action (§D.3 rules)
    cheap = ter_pct is not None and ter_pct <= 0.5
    expensive = (ter_pct is not None and ter_pct > 0.5) or not a["direct"]
    if fits is False:
        action = "AVOID"
        caveats.append(f"The category doesn't fit a {horizon} horizon / {risk} risk appetite.")
    elif hit3 < UNDERPERFORM_HIT_RATE and expensive:
        action = "REDUCE"
        alts = a.get("index_alternatives") or []
        alt_text = "; ".join(f"{x['name']} ({x['direct_ter']:.2f} %)" if x["direct_ter"] is not None else x["name"]
                             for x in alts)  # fmt: skip
        caveats.append("Switch review: consider an index fund in the same category"
                       + (f", e.g. {alt_text}" if alt_text else "")
                       + ". Switch only if the saving over your horizon beats the exit load plus the tax on the "
                         "redemption (equity: STCG 20 % within a year, LTCG 12.5 % above ₹1.25 lakh a year).")  # fmt: skip
    elif fits and cheap and a["direct"] and hit3 >= INVEST_HIT_RATE and not drift:
        action = "BUY"
    elif fits is not False and hit3 >= 0.5 and not drift:
        action = "ACCUMULATE"
    else:
        action = "HOLD"
    if reasons_missing and action == "BUY":
        action = "ACCUMULATE"
    base = {"n": n3, "p": round(hit3, 4), "ci": [round(ci[0], 4), round(ci[1], 4)],
            "description": f"quarter-end 3-year windows to {last3['end']}; effective n ≈ {n_eff3:.1f}"}  # fmt: skip
    validation = Validation(status="base_rate", n=n3, metrics={"hit_rate": round(hit3, 4),
                                                               "effective_n": round(n_eff3, 2)},
                            description="The fund's own record against its category median in past windows; no "
                                        "forecast has been scored yet.")  # fmt: skip
    method = ("§D.3 rules: TER vs category (AMFI TER file), direct vs regular, share of quarter-end 1- and 3-year "
              "windows at or above the category median (AMFI NAVs, same-plan growth peers), downside capture, "
              "quarter-end drawdown, returns-based style check (R²) and category fit; probability = the historical 3-year "
              "hit rate shrunk to the centre of its Wilson interval on the effective number of windows")  # fmt: skip
    # the point estimate is the Wilson centre, which pulls a thin record towards 50 %: 16 of 16 overlapping windows
    # (about 2 independent ones) is not evidence of a 100 % chance
    centre = _wilson_centre(hit3, n_eff3)
    return signal(
        action,
        probability=round(centre, 4),
        interval=(round(ci[0], 4), round(ci[1], 4)),
        validation=validation,
        base_rate=base,
        method=method,
    )


def _wilson_centre(p: float, n: float, z: float = 1.96) -> float:
    """(p + z²/2n) / (1 + z²/n): the centre of the Wilson interval, a hit rate shrunk towards 50 %."""
    return (p + z * z / (2 * n)) / (1 + z * z / n)


@register("fund")
async def fund_signal(instrument: str, ctx: dict[str, Any]) -> Signal:
    a = await analyse(instrument)
    return build_signal(a, await _profile(), ctx)
