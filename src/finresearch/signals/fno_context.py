"""The market-context panel on /fno (#317): trend regime, realised vs implied volatility, IV rank / percentile, term
structure, skew, put-call ratios, open-interest build-up, max pain, NSE's participant-wise F&O open interest and the
event strip, for one underlying. Arithmetic: fincalc.market_context, fincalc.options, fincalc.volatility.

Each indicator carries EVIDENCE: what it measures and what is known about its predictive value. None of them feeds
the trade-plan verdict (signals.tradeplan) and none is presented as a signal. Citation tags follow
docs/dev/RESEARCH_ROADMAP.md: [V] verified from a primary source, [U] cited from memory and not re-verified in this
change, [W] the evidence itself is weak, mixed or absent. Numbers in [] refer to that file's source list.

Data, all public: the live option chain (Groww's when connected, else NSE's); NSE's F&O bhavcopy history stored by
#306 (underlying closes from fno_iv_eod, near-month futures OHLC and open interest from fno_bhav); NSE's
participant-wise open interest (fnohist.participants); the event strip (fnobook.events).
"""

from __future__ import annotations

from datetime import date, datetime
from itertools import pairwise
from typing import Any

from finresearch.fincalc import market_context as mc

_W = "[W]"
EVIDENCE: dict[str, dict[str, str]] = {
    "trend": {
        "measures": "Where the price sits against its 20/50/200-day averages and whether they rise or fall.",
        "level": "mixed",
        "evidence": "Time-series momentum (past 1-12 month returns continuing) is documented across futures markets "
                    "(Moskowitz, Ooi & Pedersen 2012, JFE [41, V abstract]). Simple moving-average rules looked "
                    "profitable on the Dow 1897-1986 (Brock, Lakonishok & LeBaron 1992, J. Finance 47(5) [U]) but "
                    "their edge did not survive a data-snooping correction out of sample (Sullivan, Timmermann & "
                    "White 1999, J. Finance 54(5) [U]). Not tested on Indian options here: context, not a signal.",
    },
    "adx": {
        "measures": "How strong a trend is, whatever its direction (Wilder's ADX on the near-month future).",
        "level": "none verified",
        "evidence": f"Wilder (1978) defined it; no peer-reviewed study of its predictive value was found {_W}.",
    },
    "volatility": {
        "measures": "Implied volatility (what options price in) against realised volatility (what the price did).",
        "level": "documented on average",
        "evidence": "Implied variance has exceeded realised variance on average (the variance risk premium; Carr & "
                    "Wu 2009, RFS [52, U]). That is why premium sellers earn a median gain and why they suffer "
                    "rare large losses; it does not time entries. No India-specific study was verified [W].",
    },
    "iv_rank": {
        "measures": "Where today's IV sits in its past-year range (rank) and the share of days it was lower "
                    "(percentile).",
        "level": "none verified",
        "evidence": f"A practitioner filter built on the variance-risk-premium idea; no study of IV rank itself "
                    f"was verified {_W}. The pre-registered backtest's sellers used it and none passed out of sample.",
    },
    "term": {
        "measures": "Near-expiry ATM IV against next-expiry ATM IV (slope = next − near). Near above next is common "
                    "and mostly mechanical: it follows the days left to each expiry and the weekly/monthly tenor mix, so "
                    "it is shown without a reading until a clock- and event-adjusted percentile exists (roadmap P3.1).",
        "level": "weak / US only",
        "evidence": "The VIX term-structure slope predicted VIX-futures and variance-swap returns in US data "
                    "(Johnson 2017, JFQA 52(6) [U]); nothing comparable was verified for Indian options [W].",
    },
    "skew": {
        "measures": "25-delta put IV minus 25-delta call IV: the extra price of downside protection.",
        "level": "weak / US stocks only",
        "evidence": "A steep put smirk preceded lower returns for individual US stocks (Xing, Zhang & Zhao 2010, "
                    "JFQA 45(3) [U]); not shown for indices or for India [W].",
    },
    "pcr": {
        "measures": "Put open interest (or volume) ÷ call open interest (or volume) across the chain.",
        "level": "weak / contested",
        "evidence": "Option volume predicted next-week US stock returns only when split by investor type and "
                    "opening buys, which the public cannot see; ratios built from public volume carried much less "
                    "(Pan & Poteshman 2006, RFS 19(3) [U]). A contrarian or a trend reading of PCR has no "
                    "verified evidence for Indian indices [W].",
    },
    "buildup": {
        "measures": "Whether today's price move came with new positions (OI up) or with positions closing (OI "
                    "down).",
        "level": "none verified",
        "evidence": f"A practitioner classification; no peer-reviewed study of its predictive value was found "
                    f"{_W}. For options it describes premium flows, not the underlying's direction.",
    },
    "max_pain": {
        "measures": "The expiry price at which option holders, in total, would receive the least.",
        "level": "weak / contested",
        "evidence": "US optionable stocks' closes cluster near strikes on expiry dates (Ni, Pearson & Poteshman "
                    "2005, JFE 78(1) [U]), which is pinning at strikes, not a pull to max pain; no evidence for "
                    "index max pain was verified [W].",
    },
    "participants": {
        "measures": "Contracts held long and short by Clients, DIIs, FIIs and Pros (NSE's end-of-day file).",
        "level": "none verified",
        "evidence": f"Describes positioning. No study verified for this app shows that it predicts index moves "
                    f"{_W}; option counts are contracts, not delta (a long put is bearish but counts as long).",
    },
    "events": {
        "measures": "Results, RBI policy and expiry dates before the contract ends.",
        "level": "documented (risk, not direction)",
        "evidence": "Implied volatility rises into scheduled announcements and falls after (Patell & Wolfson "
                    "1979, J. Accounting & Economics 1(2) [U]): an event is a gap risk to plan for, not a signal.",
    },
}  # fmt: skip
NOT_A_SIGNAL = ("Context, not signals: none of these indicators changes the trade-plan verdict, and none has verified "
                "predictive value for Indian F&O. Each card says what it measures and what the evidence is.")  # fmt: skip
HISTORY_DAYS = 420  # calendar days of stored history read for the 200-day average (≈ 290 sessions)
STRIKES_EACH_SIDE = 8


def _f(x: Any) -> float | None:
    return None if x is None else float(x)


def stored_closes(s, symbol: str, today: date) -> list[tuple[date, float]]:
    """(day, underlying close) from the bhavcopy-derived rows (fno_iv_eod), oldest first."""
    from datetime import timedelta

    from sqlalchemy import select

    from finresearch.db.models import FnoIvEod

    q = (select(FnoIvEod.day, FnoIvEod.underlying).where(FnoIvEod.symbol == symbol.upper(),
         FnoIvEod.day >= today - timedelta(days=HISTORY_DAYS), FnoIvEod.underlying.is_not(None)).order_by(FnoIvEod.day))  # fmt: skip
    return [(d, float(u)) for d, u in s.execute(q)]


def front_futures(s, symbol: str, today: date) -> list[Any]:
    """Per stored session, the front future as the direction model reads it (direction.data.set_front_future: the
    earliest expiry after the previous session with a settlement on both days), as direction.features.DayRow, so the
    panel's ADX is roll-adjusted exactly like the model's (#354 g). Oldest first; a session without one is skipped."""
    from datetime import timedelta

    from sqlalchemy import select

    from finresearch.db.models import FnoBhav
    from finresearch.direction.data import set_front_future
    from finresearch.direction.features import DayRow

    q = (select(FnoBhav.day, FnoBhav.expiry, FnoBhav.high, FnoBhav.low, FnoBhav.close, FnoBhav.settle)
         .where(FnoBhav.symbol == symbol.upper(), FnoBhav.instrument.in_(("IDF", "STF")),
                FnoBhav.day >= today - timedelta(days=HISTORY_DAYS), FnoBhav.day <= today))  # fmt: skip
    by_day: dict[date, dict[date, tuple]] = {}
    for d, e, h, lo, c, st in s.execute(q):
        by_day.setdefault(d, {})[e] = (_f(h), _f(lo), _f(c), _f(st))
    days = sorted(by_day)
    out = []
    for d0, d1 in pairwise(days):
        r = DayRow(d1)
        if set_front_future(r, by_day[d0], by_day[d1], d0) is not None:
            out.append(r)
    return out


def futures_buildup(s, symbol: str) -> dict[str, Any] | None:
    """Build-up of each listed future on the last stored day against the stored day before (close and OI)."""
    from sqlalchemy import select

    from finresearch.db.models import FnoBhav

    days = list(s.scalars(select(FnoBhav.day).where(FnoBhav.symbol == symbol.upper(),
                FnoBhav.instrument.in_(("IDF", "STF"))).distinct().order_by(FnoBhav.day.desc()).limit(2)))  # fmt: skip
    if not days:
        return None
    rows = s.execute(select(FnoBhav.day, FnoBhav.expiry, FnoBhav.close, FnoBhav.oi).where(
        FnoBhav.symbol == symbol.upper(), FnoBhav.instrument.in_(("IDF", "STF")), FnoBhav.day.in_(days))).all()  # fmt: skip
    by = {(d, e): (_f(c), oi) for d, e, c, oi in rows}
    last, prev = days[0], days[1] if len(days) > 1 else None
    out = []
    for (d, e), (c, oi) in sorted(by.items()):
        if d != last:
            continue
        pc, poi = by.get((prev, e), (None, None)) if prev else (None, None)
        dp = None if c is None or pc is None else c - pc
        doi = None if oi is None or poi is None else oi - poi
        out.append({"expiry": e.isoformat(), "close": c, "price_change": dp, "oi": oi, "oi_change": doi,
                    "buildup": mc.buildup(dp, doi)})  # fmt: skip
    return {"day": last.isoformat(), "previous_day": prev.isoformat() if prev else None, "rows": out,
            "source": "NSE F&O bhavcopy (end of day), close and open interest against the previous stored day"}  # fmt: skip


def option_buildup_live(chain) -> dict[str, Any] | None:
    """Per strike around the money, from the live chain's premium change and OI change (NSE's chain only)."""
    if chain is None or not chain.rows or chain.underlying is None:
        return None
    rows = sorted(chain.rows, key=lambda r: r.strike)
    i = min(range(len(rows)), key=lambda j: abs(rows[j].strike - chain.underlying))
    out, known = [], False
    for r in rows[max(0, i - STRIKES_EACH_SIDE) : i + STRIKES_EACH_SIDE + 1]:
        item: dict[str, Any] = {"strike": float(r.strike)}
        for right, q in (("call", r.call), ("put", r.put)):
            dp, doi = (_f(q.change), _f(q.change_in_oi)) if q else (None, None)
            known = known or (dp is not None and doi is not None)
            item[right] = {"price_change": dp, "oi_change": doi, "oi": _f(q.oi) if q else None,
                           "buildup": mc.buildup(dp, doi)}  # fmt: skip
        out.append(item)
    if not known:
        return {"status": "unknown", "rows": out, "source": chain.source,
                "note": f"{chain.source}'s chain has no premium change or OI change: build-up unknown"}  # fmt: skip
    return {"status": "ok", "rows": out, "source": f"{chain.source} option chain (live, change since the previous close)",
            "as_of": chain.as_of.isoformat() if chain.as_of else None}  # fmt: skip


def iv_rank_input(symbol: str) -> tuple[list[tuple[date, float | None]], dict[str, Any]]:
    """The per-session IV points that IV rank and percentile read (#354 b), oldest first, with a label naming the series
    and the close it is from. Bhavcopy history (fno_iv_eod, one row per stored session) when it has MIN_DAYS 30-day IVs:
    a "flat" 30-day IV (the longest expiry standing in, fincalc.fno_history.FLAT_WITHIN) counts as missing; when the
    30-day IV covers too few of the last 252 sessions but the near-expiry ATM IV covers enough (FINNIFTY, MIDCPNIFTY:
    monthly expiries rarely bracket 30 days), the near IV is ranked and the label says so. Else the monitor's recorded
    ATM IV, one point per recorded day."""
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    from finresearch.db import session_scope
    from finresearch.db.models import FnoIvEod, IvHistory
    from finresearch.fincalc import volatility as vol
    from finresearch.signals.fno import BHAV_METHOD, LIVE_METHOD

    sym = symbol.upper()
    try:
        with session_scope() as s:
            eod = s.execute(select(FnoIvEod.day, FnoIvEod.iv_30d, FnoIvEod.iv_30d_method, FnoIvEod.near_iv)
                            .where(FnoIvEod.symbol == sym).order_by(FnoIvEod.day)).all()  # fmt: skip
    except SQLAlchemyError:
        eod = []
    if sum(r[1] is not None for r in eod) >= vol.MIN_DAYS:
        iv30 = [(d, None if v is None or m == "flat" else float(v)) for d, v, m, _ in eod]
        day = eod[-1][0].isoformat()
        info = {"source": "bhavcopy", "series": "iv_30d", "as_of": day, "method": BHAV_METHOD,
                "label": f"NSE F&O bhavcopy 30-day IV at the {day} close", "url": "https://nsearchives.nseindia.com/content/fo/"}  # fmt: skip
        st = vol.iv_stats_sessions(iv30)
        if st.status.startswith("insufficient coverage"):
            near = [(d, None if v is None else float(v)) for d, _, _, v in eod]
            if vol.iv_stats_sessions(near).status == "ok":
                return near, info | {"series": "near_iv", "label": f"NSE F&O bhavcopy near-expiry ATM IV at the {day} "
                                     f"close (fallback: the 30-day IV is on only {st.n} of {vol.WINDOW} sessions)"}  # fmt: skip
        return iv30, info
    try:
        with session_scope() as s:
            rec = s.execute(select(IvHistory.day, IvHistory.atm_iv).where(IvHistory.symbol == sym)
                            .order_by(IvHistory.day)).all()  # fmt: skip
    except SQLAlchemyError:
        rec = []
    day = rec[-1][0].isoformat() if rec else None
    return [(d, float(v)) for d, v in rec], {
        "source": "recorded", "series": "atm_iv", "as_of": day, "method": LIVE_METHOD,
        "label": f"recorded daily ATM IV (NSE option chain after the {day} close)" if day else "recorded daily ATM IV",
        "url": "https://www.nseindia.com/option-chain"}  # fmt: skip


def iv_rank(symbol: str) -> tuple[Any, dict[str, Any]]:
    """(volatility.IvStats over the last 252 sessions, the input's label) for `symbol` (#354 b)."""
    from finresearch.fincalc import volatility as vol

    points, info = iv_rank_input(symbol)
    return vol.iv_stats_sessions(points), info


def needs_network_closes(symbol: str, today: date) -> bool:
    """True when fewer than 60 underlying closes are stored (build() then falls back to NSE's recent closes)."""
    from sqlalchemy.exc import SQLAlchemyError

    from finresearch.db import session_scope

    try:
        with session_scope() as s:
            return len(stored_closes(s, symbol, today)) < 60
    except SQLAlchemyError:
        return True


def _r(x: float | None, d: int = 2) -> float | None:
    return None if x is None else round(x, d)


def trend_block(closes: list[float], spot: float | None, futs: list[Any], source: str) -> dict[str, Any]:
    last = spot if spot else (closes[-1] if closes else None)
    avgs = {n: mc.sma(closes, n) for n in (20, 50, 200)}
    slopes = {n: mc.sma_slope(closes, n) for n in (20, 50, 200)}
    from finresearch.direction.features import roll_adjusted_bars

    hs, ls, cs = roll_adjusted_bars(futs)
    a = mc.adx(hs, ls, cs) if cs else None
    reg = mc.regime(last, avgs[50], avgs[200], slopes[50], a and a["adx"]) if last else {"trend": "unknown",
                                                                                           "strength": "unknown"}  # fmt: skip
    return {"close": last, "closes": len(closes), "source": source,
            "averages": [{"days": n, "sma": _r(avgs[n]), "slope_pct_10d": _r(slopes[n], 3),
                          "distance_pct": _r(mc.distance(last, avgs[n]) if last else None, 2)} for n in (20, 50, 200)],
            "adx": None if a is None else {k: _r(v) for k, v in a.items()} | {"bars": len(cs),
                   "on": "front future (NSE bhavcopy high/low/close), ratio-adjusted at each roll as the direction "
                         "model's ADX"},
            **reg, "evidence": EVIDENCE["trend"], "adx_evidence": EVIDENCE["adx"],
            "note": None if avgs[200] is not None else f"only {len(closes)} daily closes: the 200-day average "
                    "is unknown until the F&O history covers about 10 months"}  # fmt: skip


def vol_block(chain, closes: list[float], today: date, rate: float,
              term: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    """`term` (term_block's latest stored row) gives IV − RV on matched horizons: the 30-day IV against the 20-day
    realised volatility of the same close (#354 d), not this expiry's live ATM IV (often a weekly) against RV20."""
    from finresearch.fincalc import options as o
    from finresearch.fincalc import volatility as vol

    spot = float(chain.underlying) if chain is not None and chain.underlying is not None else None
    rows = [vol.StrikeIv(float(r.strike), _f(r.call.iv) if r.call else None, _f(r.put.iv) if r.put else None)
            for r in (chain.rows if chain is not None else [])]  # fmt: skip
    atm = vol.atm_iv(rows, spot) if spot and rows else None
    from finresearch.fincalc.dates import session_clock, years_to_expiry

    # #359: T to the minute (from `now`, else 15:30 IST on `today`); an expired chain has no skew (unknown)
    t = years_to_expiry(chain.expiry, session_clock(today, now)) if chain is not None else None
    skew = vol.skew_25d(rows, spot, t, rate) if spot and rows and t and t > 0 else None
    rv20 = o.realised_vol(closes, 20) * 100 if len(closes) >= 22 else None
    rv60 = o.realised_vol(closes, 60) * 100 if len(closes) >= 62 else None
    st, src = iv_rank(chain.symbol if chain is not None else "")
    iv = atm[1] if atm else None
    t30, rv_eod = (term or {}).get("iv_30d"), (term or {}).get("rv_20")
    matched = None if t30 is None or rv_eod is None else t30 - rv_eod
    return {"atm_iv": _r(iv), "atm_strike": atm[0] if atm else None, "expiry": chain.expiry.isoformat() if chain else None,
            "realised_20d": _r(rv20), "realised_60d": _r(rv60), "iv_minus_rv20": _r(matched),
            "iv_minus_rv20_label": None if matched is None else f"30-day IV {t30:.1f}% − 20-day realised {rv_eod:.1f}% "
                                   f"(NSE F&O bhavcopy, both at the {term['day']} close)",  # type: ignore[index]
            "iv_rank": _r(st.rank, 4), "iv_percentile": _r(st.percentile, 4), "iv_days": st.n, "iv_status": st.status,
            "iv_source": src["label"], "skew_25d": _r(skew, 3), "evidence": EVIDENCE["volatility"],
            "rank_evidence": EVIDENCE["iv_rank"], "skew_evidence": EVIDENCE["skew"]}  # fmt: skip


def term_block(s, symbol: str) -> dict[str, Any] | None:
    from sqlalchemy import select

    from finresearch.db.models import FnoIvEod

    r = s.scalars(
        select(FnoIvEod).where(FnoIvEod.symbol == symbol.upper()).order_by(FnoIvEod.day.desc()).limit(1)
    ).first()
    if r is None:
        return None
    near, nxt = _f(r.near_iv), _f(r.next_iv)
    slope = None if near is None or nxt is None else round(nxt - near, 3)
    # neutral words only (#354 c): "inverted = stress now" fired on 36 % of NIFTY days, mostly from the expiry clock
    shape = None if slope is None else "flat" if slope == 0 else "near above next" if slope < 0 else "next above near"  # fmt: skip
    return {"day": r.day.isoformat(), "near_expiry": r.near_expiry.isoformat() if r.near_expiry else None,
            "near_iv": near, "next_expiry": r.next_expiry.isoformat() if r.next_expiry else None, "next_iv": nxt,
            "iv_30d": _f(r.iv_30d), "rv_20": _f(r.rv_20), "shape": shape, "slope": slope, "evidence": EVIDENCE["term"],
            "source": "NSE F&O bhavcopy (end of day), fnohist.derive"}  # fmt: skip


def chain_block(chain) -> dict[str, Any]:
    put_v = sum(float(r.put.volume or 0) for r in chain.rows if r.put)
    call_v = sum(float(r.call.volume or 0) for r in chain.rows if r.call)
    has_v = any(r.put and r.put.volume is not None for r in chain.rows)
    pcr_oi = chain.pcr()
    return {"expiry": chain.expiry.isoformat(), "pcr_oi": _r(_f(pcr_oi), 3),
            "pcr_volume": _r(mc.pcr(put_v, call_v), 3) if has_v else None,
            "max_pain": _f(chain.max_pain()), "spot": _f(chain.underlying), "source": chain.source,
            "as_of": chain.as_of.isoformat() if chain.as_of else None,
            "pcr_evidence": EVIDENCE["pcr"], "max_pain_evidence": EVIDENCE["max_pain"]}  # fmt: skip


def build(chain, *, today: date, rate: float, network_closes: list[float] | None = None,
          events: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    """The whole panel for the chain's underlying. Stored history first (no network); `network_closes` (NSE's ~90
    days) only when fewer than 60 closes are stored. A block whose data is missing says so (never zeros)."""
    from sqlalchemy.exc import SQLAlchemyError

    from finresearch.db import session_scope
    from finresearch.fnohist import participants as pt

    sym = chain.symbol.upper()
    stored: list[tuple[date, float]] = []
    futs: list[Any] = []
    term = fut_bu = parts = None
    try:
        with session_scope() as s:
            stored = stored_closes(s, sym, today)
            futs = front_futures(s, sym, today)
            term = term_block(s, sym)
            fut_bu = futures_buildup(s, sym)
            parts = pt.latest(s)
    except SQLAlchemyError:  # a database without the #306/#317 tables: every stored block is unknown
        pass
    closes = [c for _, c in stored]
    source = f"NSE F&O bhavcopy underlying closes, last {stored[-1][0]:%d-%b-%Y}" if stored else "none stored"
    if len(closes) < 60 and network_closes:
        closes, source = (
            network_closes,
            f"NSE daily closes (last {len(network_closes)} sessions, fetched now)",
        )
    spot = _f(chain.underlying)
    return {
        "symbol": sym, "today": today.isoformat(), "not_a_signal": NOT_A_SIGNAL,
        "trend": trend_block(closes, spot, futs, source),
        "volatility": vol_block(chain, closes, today, rate, term=term, now=now),
        "term": term or {"status": "unknown", "note": "no stored bhavcopy IV for this underlying",
                         "evidence": EVIDENCE["term"]},
        "chain": chain_block(chain),
        "buildup": {"futures": fut_bu, "options": option_buildup_live(chain), "evidence": EVIDENCE["buildup"],
                    "method": mc.BUILDUP_METHOD},
        "participants": (parts or {"status": "unknown", "note": "no participant-wise OI stored yet (fetched each "
                         "evening by the nightly F&O job)"}) | {"evidence": EVIDENCE["participants"],
                         "url": "https://www.nseindia.com/all-reports-derivatives"},
        "events": (events or {"status": "unknown"}) | {"evidence": EVIDENCE["events"]},
    }  # fmt: skip
