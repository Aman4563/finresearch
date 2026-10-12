"""The F&O book's live work (#308): price the user's Groww F&O positions, quote paper entries and exits, and the
monitor step that marks paper trades, settles expired ones and checks exit rules.

Groww call budget: everything goes through `adapters.groww_market.MARKET` (shared rate slots per Groww category,
in-session LTP cache of 15 s, option chains cached 30 s). The monitor step runs its market pass at most every
MTM_EVERY in session and only when there is an open paper trade or an armed exit rule: one LTP call (Live Data) per 50
open legs, plus one option-chain call per underlying + expiry that has an armed delta rule or legs Groww could not
price. No open trades and no armed rules: no calls and no rows."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from finresearch.fincalc.dates import to_ist
from finresearch.fnobook import book, exits
from finresearch.fnobook import events as ev
from finresearch.fnobook import positions as pos

log = logging.getLogger(__name__)

MTM_EVERY = timedelta(minutes=5)
SETTLE_RETRY = timedelta(hours=1)
RATE = 0.065  # the greeks' rate when none is passed (signals.fno.RATE)
GROWW_LTP = "Groww LTP"
_LAST: dict[str, datetime] = {}


def _market() -> Any:
    from finresearch.adapters import groww_market

    return groww_market.MARKET


def _sources() -> Any:
    from finresearch.fnobook import sources

    return sources.SOURCES


def _phase(now: datetime) -> str:
    from finresearch.adapters.groww_market import phase

    return phase(now)


def _t(expiry: date, today: date) -> float:
    return max((expiry - today).days, 0.5) / 365


async def _instruments(market: Any) -> Any:
    try:
        return await market.instruments()
    except Exception as e:
        log.info("Groww instruments file unavailable: %s", type(e).__name__)
        return None


async def _fno_ltp(market: Any, symbols: list[str]) -> dict[str, float]:
    """{trading symbol: LTP} from Groww (segment FNO) for the symbols it priced; {} without a session or out of
    session (the cache answers what it has)."""
    if not symbols or not market.token():
        return {}
    keys = {f"NSE_{s}": s for s in symbols}
    try:
        got = await market.ltp(list(keys), segment="FNO")
    except Exception as e:
        log.info("Groww F&O LTP failed: %s", type(e).__name__)
        return {}
    return {keys[k]: v[0] for k, v in got.items() if k in keys}


async def _chain(sources: Any, symbol: str, expiry: date) -> Any:
    try:
        return await sources.chain(symbol, expiry)
    except Exception as e:
        log.info("option chain %s %s unavailable: %s", symbol, expiry, type(e).__name__)
        return None


def _chain_price(chain: Any, right: str, strike: float) -> float | None:
    if chain is None or right == "future":
        return None
    for r in chain.rows:
        if float(r.strike) == strike:
            q = r.call if right == "call" else r.put
            return float(q.last_price) if q is not None and q.last_price else None
    return None


# --------------------------------------------------------------------------- real positions
def stored_positions() -> dict[str, Any]:
    """The F&O positions the last Groww sync or in-session poll read: {"status", "at", "rows"}."""
    from finresearch.db import session_scope
    from finresearch.db.models import BrokerConnection

    with session_scope() as s:
        row = s.get(BrokerConnection, "groww")
        if row is None or not row.enabled:
            return {"status": "not_connected", "at": None, "rows": []}
        got = (row.state or {}).get("fno_positions")
    if not got:
        return {"status": "not_read", "at": None, "rows": []}
    return {"status": "ok", "at": got.get("at"), "rows": list(got.get("rows") or [])}


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


async def positions_view(now: datetime, *, rate: float = RATE, market: Any = None, sources: Any = None,
                         stored: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """The user's F&O positions grouped by underlying + expiry, priced live (see `fnobook.positions`)."""
    market, sources = market or _market(), sources or _sources()
    st = stored if stored is not None else stored_positions()
    today = to_ist(now).date()
    inst = await _instruments(market) if st["rows"] else None
    legs: list[dict[str, Any]] = []
    for r in st["rows"]:
        c = pos.contract(inst.contract(r.get("symbol") or "") if inst else None)
        known = bool(r.get("direction_known"))
        qty = _f(r.get("quantity"))
        legs.append({"symbol": r.get("symbol"), "exchange": r.get("exchange") or "NSE", "product": r.get("product"),
                     "quantity": qty, "qty": qty if known else None, "direction_known": known, "note": r.get("note"),
                     "avg_price": _f(r.get("avg_price")), "realised_pnl": _f(r.get("pnl")),
                     "contract_known": c is not None, **(c or {"underlying": None, "expiry": None, "strike": None,
                                                              "right": None, "lot_size": None})})  # fmt: skip
    ltp = await _fno_ltp(market, [x["symbol"] for x in legs if x["symbol"]])
    groups: dict[str, list[dict[str, Any]]] = {}
    for x in legs:
        key = pos.group_key(x["underlying"], x["expiry"]) if x["contract_known"] else "unresolved"
        groups.setdefault(key, []).append(x)
    out = []
    for key, gl in groups.items():
        out.append(await _group(key, gl, ltp, today, rate, sources))
    return {"status": st["status"], "read_at": st["at"], "groups": out, "ltp_source": GROWW_LTP if ltp else None,
            "as_of": now.isoformat(),
            "notes": ["Read-only: positions come from Groww's positions API (segment FNO); nothing here can trade.",
                      "Unrealised P&L = (LTP − Groww's net average price) × signed quantity [U: whether net_price "
                      "is the open quantity's average is not stated in Groww's docs].",
                      "Greeks: Black-Scholes at the option chain's IV for the strike (else the IV implied by the "
                      "LTP), per position (× signed units)."]}  # fmt: skip


async def _group(key: str, gl: list[dict[str, Any]], ltp: dict[str, float], today: date, rate: float,
                 sources: Any) -> dict[str, Any]:  # fmt: skip
    first = gl[0]
    chain = None
    if first["contract_known"]:
        chain = await _chain(sources, first["underlying"], first["expiry"])
    spot = _f(getattr(chain, "underlying", None)) if chain is not None else None
    t = _t(first["expiry"], today) if first["contract_known"] else None
    tot = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    greeks_known, pnl, pnl_known = True, 0.0, True
    for x in gl:
        x["ltp"] = ltp.get(x["symbol"])
        x["unrealised_pnl"] = (None if x["qty"] is None or x["ltp"] is None or x["avg_price"] is None
                               else round((x["ltp"] - x["avg_price"]) * x["qty"], 2))  # fmt: skip
        if x["unrealised_pnl"] is None:
            pnl_known = False
        else:
            pnl += x["unrealised_pnl"]
        g = None
        if x["contract_known"] and x["qty"] is not None and t is not None:
            iv = None
            if x["right"] != "future":
                iv = pos.chain_iv(chain, x["right"], x["strike"]) or pos.implied(x["right"], x["ltp"], spot, x["strike"],
                                                                               t, rate)  # fmt: skip
            g = pos.leg_greeks(x["right"], x["strike"] or 0.0, spot, t, rate, iv)
            x["iv"] = None if iv is None else round(iv, 4)
        x["greeks"] = None if g is None else {k: round(getattr(g, k) * x["qty"], 4) for k in tot}
        if g is None:
            greeks_known = False
        else:
            for k in tot:
                tot[k] += getattr(g, k) * x["qty"]
    ml, why = pos.max_loss(gl, spot) if first["contract_known"] else (None, "contract not in the instruments file")
    return {"key": key, "underlying": first["underlying"], "expiry": first["expiry"].isoformat() if first["expiry"]
            else None, "days_to_expiry": (first["expiry"] - today).days if first["expiry"] else None, "spot": spot,
            "chain_source": getattr(chain, "source", None), "legs": [{**x, "expiry": x["expiry"].isoformat()
            if x["expiry"] else None} for x in gl], "unrealised_pnl": round(pnl, 2) if pnl_known else None,
            "net_greeks": {k: round(v, 4) for k, v in tot.items()} if greeks_known else None,
            "max_loss": None if ml is None else round(ml, 2), "max_loss_note": why or None}  # fmt: skip


# --------------------------------------------------------------------------- paper entries and exits
class OutOfSession(ValueError):
    """Paper trades are entered and closed only while the market is open, at live prices."""


def _kind(right: str) -> str:
    return {"call": "CE", "put": "PE", "future": "FUT"}[right]


async def quotes(symbol: str, expiry: date, legs: list[tuple[str, float]], now: datetime, *, market: Any = None,
                 sources: Any = None) -> tuple[list[book.Quote], Any]:  # fmt: skip
    """Live reference prices for legs [(right, strike)]: Groww's LTP for the contract when Groww answers, else the
    option chain's last price (Groww's chain, else NSE's). Raises OutOfSession outside 09:15-16:00 IST: a paper fill
    at a stale price would flatter the results."""
    if _phase(now) not in ("open", "closing"):
        raise OutOfSession("paper trades are entered and closed only in market hours (09:15-15:30 IST), at live "
                           "prices")  # fmt: skip
    market, sources = market or _market(), sources or _sources()
    inst = await _instruments(market)
    syms = [inst.fno_symbol(symbol, expiry, k, _kind(r)) if inst else None for r, k in legs]
    ltp = await _fno_ltp(market, [s for s in syms if s])
    chain = await _chain(sources, symbol, expiry)
    out = []
    for (r, k), ts in zip(legs, syms, strict=True):
        if ts and ts in ltp:
            out.append(book.Quote(ltp[ts], GROWW_LTP, ts))
        else:
            p = _chain_price(chain, r, k)
            src = f"{getattr(chain, 'source', 'NSE') or 'NSE'} option chain last price"
            out.append(book.Quote(p, src, ts))
    return out, chain


def paper_short_deltas(st: Any, chain: Any, today: date, rate: float = RATE) -> list[float | None]:
    """|delta| inputs for the delta rule: one per short option leg, None when there is no spot or IV."""
    spot = _f(getattr(chain, "underlying", None)) if chain is not None else None
    t = _t(st.expiry, today)
    out: list[float | None] = []
    for x in st.legs:
        if x.side != "sell" or x.right == "future":
            continue
        k = float(x.strike)
        iv = pos.chain_iv(chain, x.right, k) or pos.implied(x.right, _f(x.last_price), spot, k, t, rate)
        g = pos.leg_greeks(x.right, k, spot, t, rate, iv)
        out.append(None if g is None else g.delta)
    return out


# --------------------------------------------------------------------------- the monitor step
def _due(key: str, now: datetime, every: timedelta) -> bool:
    last = _LAST.get(key)
    if last is not None and now - last < every:
        return False
    _LAST[key] = now
    return True


async def step(now: datetime, *, market: Any = None, sources: Any = None) -> dict[str, int]:
    """The monitor tick's F&O book work. Nothing (no calls, no rows) without open paper trades or armed rules."""
    from finresearch.db import session_scope
    from finresearch.db.models import FnoExitRule, PaperStrategy

    with session_scope() as s:
        live = s.scalar(select(PaperStrategy.id).where(PaperStrategy.status.in_((book.OPEN, book.AWAITING))).limit(1))
        armed = s.scalar(select(FnoExitRule.id).where(FnoExitRule.status == "armed").limit(1))
    if live is None and armed is None:
        return {}
    market, sources = market or _market(), sources or _sources()
    out: dict[str, int] = {}
    if live is not None:
        out |= await settle_step(now, sources)
    if _phase(now) in ("open", "closing") and _due("market", now, MTM_EVERY):
        out |= await market_pass(now, market, sources)
    return {k: v for k, v in out.items() if v}


def _settle_due(expiry: date, now: datetime) -> bool:
    ist = to_ist(now)
    return ist.date() > expiry or (ist.date() == expiry and _phase(now) in ("after", "closed"))


async def settle_step(now: datetime, sources: Any) -> dict[str, int]:
    """Settle paper trades whose expiry has passed at the underlying's close on the expiry day; until NSE publishes
    it the trade waits (status awaiting_settlement), retried hourly."""
    from finresearch.db import session_scope
    from finresearch.db.models import PaperStrategy

    with session_scope() as s:
        due = [(r.id, r.symbol, r.expiry) for r in s.scalars(select(PaperStrategy).where(
            PaperStrategy.status.in_((book.OPEN, book.AWAITING)))) if _settle_due(r.expiry, now)]  # fmt: skip
    out = {"paper_settled": 0, "paper_awaiting": 0}
    for sid, sym, exp in due:
        if not _due(f"settle:{sid}", now, SETTLE_RETRY):
            continue
        try:
            got = await sources.settlement(sym, exp)
        except Exception as e:
            log.info("settlement price %s %s unavailable: %s", sym, exp, type(e).__name__)
            got = None
        with session_scope() as s:
            st = s.get(PaperStrategy, sid, with_for_update=True)
            if st is None or st.status == book.CLOSED:
                continue
            if got is None:
                st.status = book.AWAITING
                st.note = f"waiting for NSE's close of {sym} on {exp:%d-%b-%Y} to settle (checked {now:%d-%b %H:%M} UTC)"
                out["paper_awaiting"] += 1
            else:
                book.settle_at_expiry(st, got[0], got[1], now)
                st.note = None
                out["paper_settled"] += 1
    return out


async def market_pass(now: datetime, market: Any, sources: Any, rate: float = RATE) -> dict[str, int]:
    """Mark open paper trades (Groww LTP batch; the chain's last price for legs Groww did not price) and check every
    armed exit rule. One rule check per pass; a rule that fires records one alert and never fires again."""
    from finresearch.db import session_scope
    from finresearch.db.models import FnoExitRule, PaperStrategy

    today = to_ist(now).date()
    with session_scope() as s:
        rules = list(s.scalars(select(FnoExitRule).where(FnoExitRule.status == "armed")))
        need = {exits.target_key(r): set() for r in rules}
        for r in rules:
            need[exits.target_key(r)].add(r.kind)
        opens = list(s.scalars(select(PaperStrategy).where(PaperStrategy.status == book.OPEN,
                                                           PaperStrategy.expiry >= today)))  # fmt: skip
        legs = {st.id: [(x.id, x.right, float(x.strike), x.trading_symbol) for x in st.legs] for st in opens}
        heads = {st.id: (st.symbol, st.expiry) for st in opens}
    ltp = await _fno_ltp(market, [ts for ls in legs.values() for *_, ts in ls if ts])
    chains: dict[tuple[str, date], Any] = {}

    async def chain(sym: str, exp: date) -> Any:
        if (sym, exp) not in chains:
            chains[(sym, exp)] = await _chain(sources, sym, exp)
        return chains[(sym, exp)]

    marks: dict[int, tuple[dict[int, float], str]] = {}
    for sid, ls in legs.items():
        prices, srcs = {}, set()
        for lid, right, k, ts in ls:
            if ts and ts in ltp:
                prices[lid] = ltp[ts]
                srcs.add(GROWW_LTP)
            else:
                c = await chain(*heads[sid])
                p = _chain_price(c, right, k)
                if p is not None:
                    prices[lid] = p
                    srcs.add(f"{getattr(c, 'source', None) or 'NSE'} chain")
        if "delta" in need.get(f"paper:{sid}", ()):
            await chain(*heads[sid])
        marks[sid] = (prices, ", ".join(sorted(srcs)))
    snaps: dict[str, exits.Snapshot] = {}
    with session_scope() as s:
        for sid, (prices, src) in marks.items():
            st = s.get(PaperStrategy, sid)
            book.mark(st, prices, src or "no price", now)
            key = f"paper:{sid}"
            if key in need:
                snaps[key] = exits.Snapshot(label=f"Paper #{sid} {st.symbol} {st.expiry:%d-%b}", symbol=st.symbol,
                                            expiry=st.expiry, today=today, gross_pnl=_f(st.mtm_gross),
                                            base=abs(book.pp.net_premium(book.legs_of(st))) or None,
                                            short_deltas=paper_short_deltas(st, chains.get(heads[sid]), today, rate),
                                            path=f"/fno/paper#{sid}")  # fmt: skip
    if any(k.startswith("real:") for k in need):
        snaps |= await real_snapshots(now, market, sources, rate)
    for key, p in snaps.items():
        if "event" in need.get(key, ()):
            p.events = await ev.event_dates(p.symbol, today, sources)
    with session_scope() as s:
        locked = list(s.scalars(select(FnoExitRule).where(FnoExitRule.status == "armed")
                                .with_for_update(skip_locked=True)))  # fmt: skip
        res = exits.run(s, locked, snaps, now)
    return {"paper_marked": len(marks), "exit_rules_fired": res.get("fired", 0)}


async def real_snapshots(now: datetime, market: Any, sources: Any, rate: float = RATE) -> dict[str, exits.Snapshot]:
    """Exit-rule snapshots of the user's Groww F&O position groups (underlying + expiry)."""
    view = await positions_view(now, rate=rate, market=market, sources=sources)
    today = to_ist(now).date()
    out = {}
    for g in view["groups"]:
        if g["key"] == "unresolved":
            continue
        opts = [x for x in g["legs"] if x["right"] != "future"]
        base = None if any(x["qty"] is None or x["avg_price"] is None for x in opts) else (
            abs(sum(x["avg_price"] * x["qty"] for x in opts)) or None)  # fmt: skip
        deltas = [None if x["greeks"] is None or not x["qty"] else x["greeks"]["delta"] / x["qty"]
                  for x in opts if x["qty"] is None or x["qty"] < 0]  # fmt: skip
        out[f"real:{g['key']}"] = exits.Snapshot(label=f"{g['underlying']} {date.fromisoformat(g['expiry']):%d-%b} "
                                                 "(Groww)", symbol=g["underlying"], expiry=date.fromisoformat(g["expiry"]),
                                                 today=today, gross_pnl=g["unrealised_pnl"], base=base,
                                                 short_deltas=deltas, path=f"/fno?symbol={g['underlying']}")  # fmt: skip
    return out
