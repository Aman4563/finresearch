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

from datetime import date
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
        "measures": "Near-expiry IV against next-expiry IV: an inverted curve (near above next) means stress now.",
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


def near_futures(s, symbol: str, today: date) -> list[dict[str, Any]]:
    """Per stored day, the nearest-expiry future (the near month): day, expiry, high, low, close, oi, chg_oi."""
    from datetime import timedelta

    from sqlalchemy import select

    from finresearch.db.models import FnoBhav

    q = (select(FnoBhav.day, FnoBhav.expiry, FnoBhav.high, FnoBhav.low, FnoBhav.close, FnoBhav.oi, FnoBhav.chg_oi)
         .where(FnoBhav.symbol == symbol.upper(), FnoBhav.instrument.in_(("IDF", "STF")),
                FnoBhav.day >= today - timedelta(days=HISTORY_DAYS)).order_by(FnoBhav.day, FnoBhav.expiry))  # fmt: skip
    out: dict[date, dict[str, Any]] = {}
    for d, e, h, lo, c, oi, chg in s.execute(q):
        if d not in out and e >= d:
            out[d] = {
                "day": d,
                "expiry": e,
                "high": _f(h),
                "low": _f(lo),
                "close": _f(c),
                "oi": oi,
                "chg_oi": chg,
            }
    return list(out.values())


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


def _r(x: float | None, d: int = 2) -> float | None:
    return None if x is None else round(x, d)


def trend_block(
    closes: list[float], spot: float | None, futs: list[dict[str, Any]], source: str
) -> dict[str, Any]:
    last = spot if spot else (closes[-1] if closes else None)
    avgs = {n: mc.sma(closes, n) for n in (20, 50, 200)}
    slopes = {n: mc.sma_slope(closes, n) for n in (20, 50, 200)}
    bars = [x for x in futs if x["high"] is not None and x["low"] is not None and x["close"] is not None]
    a = (
        mc.adx([x["high"] for x in bars], [x["low"] for x in bars], [x["close"] for x in bars])
        if bars
        else None
    )
    reg = mc.regime(last, avgs[50], avgs[200], slopes[50], a and a["adx"]) if last else {"trend": "unknown",
                                                                                           "strength": "unknown"}  # fmt: skip
    return {"close": last, "closes": len(closes), "source": source,
            "averages": [{"days": n, "sma": _r(avgs[n]), "slope_pct_10d": _r(slopes[n], 3),
                          "distance_pct": _r(mc.distance(last, avgs[n]) if last else None, 2)} for n in (20, 50, 200)],
            "adx": None if a is None else {k: _r(v) for k, v in a.items()} | {"bars": len(bars),
                   "on": "near-month future (NSE bhavcopy high/low/close; rolls at each expiry)"},
            **reg, "evidence": EVIDENCE["trend"], "adx_evidence": EVIDENCE["adx"],
            "note": None if avgs[200] is not None else f"only {len(closes)} daily closes: the 200-day average "
                    "is unknown until the F&O history covers about 10 months"}  # fmt: skip


def vol_block(chain, closes: list[float], today: date, rate: float) -> dict[str, Any]:
    from finresearch.fincalc import options as o
    from finresearch.fincalc import volatility as vol
    from finresearch.signals.fno import iv_series_info

    spot = float(chain.underlying) if chain is not None and chain.underlying is not None else None
    rows = [vol.StrikeIv(float(r.strike), _f(r.call.iv) if r.call else None, _f(r.put.iv) if r.put else None)
            for r in (chain.rows if chain is not None else [])]  # fmt: skip
    atm = vol.atm_iv(rows, spot) if spot and rows else None
    t = max((chain.expiry - today).days, 0.5) / 365 if chain is not None else None
    skew = vol.skew_25d(rows, spot, t, rate) if spot and rows and t else None
    rv20 = o.realised_vol(closes, 20) * 100 if len(closes) >= 22 else None
    rv60 = o.realised_vol(closes, 60) * 100 if len(closes) >= 62 else None
    series, src = iv_series_info(chain.symbol if chain is not None else "")
    st = vol.iv_stats([v for _, v, _ in series])
    iv = atm[1] if atm else None
    return {"atm_iv": _r(iv), "atm_strike": atm[0] if atm else None, "expiry": chain.expiry.isoformat() if chain else None,
            "realised_20d": _r(rv20), "realised_60d": _r(rv60), "iv_minus_rv20": _r(iv - rv20) if iv and rv20 else None,
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
    shape = (
        None
        if near is None or nxt is None
        else ("inverted (near above next)" if near > nxt else "normal (upward)")
    )
    return {"day": r.day.isoformat(), "near_expiry": r.near_expiry.isoformat() if r.near_expiry else None,
            "near_iv": near, "next_expiry": r.next_expiry.isoformat() if r.next_expiry else None, "next_iv": nxt,
            "iv_30d": _f(r.iv_30d), "shape": shape, "evidence": EVIDENCE["term"],
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
          events: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """The whole panel for the chain's underlying. Stored history first (no network); `network_closes` (NSE's ~90
    days) only when fewer than 60 closes are stored. A block whose data is missing says so (never zeros)."""
    from sqlalchemy.exc import SQLAlchemyError

    from finresearch.db import session_scope
    from finresearch.fnohist import participants as pt

    sym = chain.symbol.upper()
    stored: list[tuple[date, float]] = []
    futs: list[dict[str, Any]] = []
    term = fut_bu = parts = None
    try:
        with session_scope() as s:
            stored = stored_closes(s, sym, today)
            futs = near_futures(s, sym, today)
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
        "volatility": vol_block(chain, closes, today, rate),
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
