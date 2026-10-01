"""What the API, the alert metrics and the morning brief show about disclosures, read from the database only.

Three states per source (DATA-004): "ok" (a good read recent enough; an empty result then really means "none"),
"unavailable" (never read, or the last good read is older than the feed's MAX_AGE, or the F&O ban list is not for
today's trade date; the last failure is shown) and, for a BSE-only stock, "not_covered" (NSE's feeds do not list it).
Every state carries the source URL and the publisher's as-of date.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finresearch.adapters.nse_disclosures import (
    FNO_BAN_PAGE,
    PLEDGE_PAGE,
    RATING_PAGE,
    SURVEILLANCE_PAGE,
    issuer_code,
)
from finresearch.disclosures import store
from finresearch.fincalc.dates import to_ist
from finresearch.fincalc.disclosures import net_insider, pledge_change

MAX_AGE = {"asm": timedelta(days=4), "gsm": timedelta(days=4), "credit_ratings": timedelta(days=4),
           "sebi_orders": timedelta(days=4), "fno_ban": timedelta(days=4), "pledge": timedelta(days=10),
           "pit": timedelta(days=4), "sast": timedelta(days=10), "deals": timedelta(days=4)}  # fmt: skip
LABEL = {"asm": "NSE ASM list", "gsm": "NSE GSM list", "fno_ban": "NSE F&O ban list", "credit_ratings":
         "credit-rating filings (NSE)", "sebi_orders": "SEBI orders (RSS)", "pledge": "promoter pledge (NSE)",
         "pit": "insider trades (NSE PIT filings)", "sast": "SAST Reg 29 (NSE)", "deals": "bulk/block deals (NSE)"}  # fmt: skip
PAGE = {"asm": SURVEILLANCE_PAGE, "gsm": "https://www.nseindia.com/reports/gsm", "fno_ban": FNO_BAN_PAGE,
        "credit_ratings": RATING_PAGE, "pledge": PLEDGE_PAGE}  # fmt: skip
INSIDER_DAYS = 90
DEAL_DAYS_RECENT = 5
RATING_DAYS = 365
NEW_ITEM_DAYS = 7  # a rule's first check fires on an adverse rating action or a SEBI order at most this old


def _iso(d: Any) -> str | None:
    return d.isoformat() if isinstance(d, date | datetime) else d


def feed_state(row: Any, now: datetime, dataset: str) -> dict[str, Any]:
    """The state of one feed (see the module doc)."""
    base = {
        "source": LABEL.get(dataset, dataset),
        "source_url": (row.source_url if row else None) or PAGE.get(dataset),
    }
    if row is None or row.ok_at is None:
        return {**base, "state": "unavailable", "reason": (row.error if row and row.error else "not read yet"),
                "error_at": _iso(row.error_at) if row else None, "as_of": None, "fetched_at": None}  # fmt: skip
    failing = row.error_at is not None and row.error_at > row.ok_at
    out = {**base, "as_of": row.as_of, "fetched_at": _iso(row.ok_at), "error": row.error if failing else None,
           "error_at": _iso(row.error_at) if failing else None}  # fmt: skip
    if now - row.ok_at > MAX_AGE.get(dataset, timedelta(days=4)):
        why = row.error if failing else "no refresh since"
        return {
            **out,
            "state": "unavailable",
            "reason": f"last good read {to_ist(row.ok_at):%d %b %Y} ({why})",
        }
    if dataset == "fno_ban" and row.as_of and row.as_of < to_ist(now).date().isoformat():
        return {**out, "state": "unavailable",
                "reason": f"the ban list read is for trade date {row.as_of}; today's has not been read yet"}  # fmt: skip
    return {**out, "state": "ok"}


def _ok(st: dict[str, Any]) -> bool:
    return st["state"] == "ok"


# --------------------------------------------------------------------------- the stock's ISIN
def resolve_isin(s: Session, symbol: str, isin: str | None = None) -> str | None:
    """The stock's ISIN: given, else the tracked set, a surveillance row, or a stored insider filing."""
    from sqlalchemy import select

    from finresearch.db.models import DisclosureRecord

    if isin:
        return isin.upper()
    t = store.tracked(s).stocks.get(symbol)
    if t and t.get("isin"):
        return t["isin"]
    for ds in ("asm", "gsm"):
        row = store.feed(s, ds)
        for r in ((row.payload or {}).get("rows") or []) if row else []:
            if r.get("symbol") == symbol and r.get("isin"):
                return r["isin"]
    return s.scalar(select(DisclosureRecord.isin).where(DisclosureRecord.symbol == symbol,
                                                        DisclosureRecord.isin.is_not(None)).limit(1))  # fmt: skip


# --------------------------------------------------------------------------- surveillance and F&O ban
def surveillance(s: Session, symbol: str, now: datetime, isin: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for ds in ("asm", "gsm"):
        row = store.feed(s, ds)
        st = feed_state(row, now, ds)
        hits = []
        if row is not None and row.ok_at is not None:
            for r in (row.payload or {}).get("rows") or []:
                if r.get("symbol") == symbol or (isin and r.get("isin") == isin):
                    hits.append(r)
        out[ds] = {**st, "entries": hits if _ok(st) or hits else []}
    row = store.feed(s, "fno_ban")
    st = feed_state(row, now, "fno_ban")
    syms = set(((row.payload or {}).get("symbols") or []) if row else [])
    out["fno_ban"] = {**st, "trade_date": row.as_of if row else None,
                      "in_ban": (symbol in syms) if _ok(st) else None}  # fmt: skip
    return out


def surv_label(e: dict[str, Any]) -> str:
    if e.get("framework") == "ASM":
        return f"ASM {e.get('term') or ''} Stage {e.get('stage') or '?'}".replace("  ", " ")
    parts = [f"GSM Stage {e['stage']}" if e.get("stage") is not None else "GSM"]
    if e.get("esm"):
        parts.append(f"ESM Stage {e['esm']}")
    if e.get("ibc"):
        parts.append("IBC (insolvency)")
    return " · ".join(parts)


def stage_text(surv: dict[str, Any]) -> str | None:
    """ "none" or the stages joined ("ASM long-term Stage I; GSM Stage 0 · IBC (insolvency)"); None when either list is
    unavailable (a change cannot be told from a failure)."""
    if not (_ok(surv["asm"]) and _ok(surv["gsm"])):
        return None
    labels = sorted(surv_label(e) for e in surv["asm"]["entries"] + surv["gsm"]["entries"])
    return "; ".join(labels) or "none"


# --------------------------------------------------------------------------- pledge
def pledge(s: Session, symbol: str, now: datetime) -> dict[str, Any]:
    row = store.feed(s, "pledge", symbol)
    st = feed_state(row, now, "pledge")
    hist = store.records(s, "pledge", symbol=symbol)
    quarters = [(r.day, _dec(r.data.get("pct_of_promoter"))) for r in hist]
    change, prev_q, _ = pledge_change(quarters)
    payload = (row.payload or {}) if row else {}
    latest = payload.get("latest") if row and row.ok_at else None
    return {**st, "latest": latest, "no_record": bool(payload.get("no_record")),
            "change_pp": None if change is None else str(change), "prev_quarter": _iso(prev_q),
            "history": [{"quarter_end": _iso(r.day), "pct_of_promoter": r.data.get("pct_of_promoter"),
                         "pct_of_equity": r.data.get("pct_of_equity"), "mismatch": r.data.get("mismatch")}
                        for r in hist[:8]],
            "basis": "Promoter shares encumbered at the quarter end (pledge and other encumbrances under SEBI SAST "
                     "Regulation 31), recomputed from NSE's share counts: % of promoter holding and % of equity. "
                     "The change compares the two latest quarters recorded here."}  # fmt: skip


# --------------------------------------------------------------------------- insider, SAST, deals
def insider(s: Session, symbol: str, now: datetime, days: int = INSIDER_DAYS) -> dict[str, Any]:
    today = to_ist(now).date()
    row = store.feed(s, "pit", symbol)
    st = feed_state(row, now, "pit")
    start = today - timedelta(days=days - 1)
    recs = store.records(s, "pit", symbol=symbol, since=start - timedelta(days=30))
    superseded = {r.data.get("prev_app_id") for r in recs if r.data.get("prev_app_id")}
    rows = [r.data for r in recs if r.data.get("app_id") not in superseded]
    payload = (row.payload or {}) if row else {}
    win = payload.get("window") or [None, None]
    problems = []
    if payload.get("failed"):
        problems.append(f"{len(payload['failed'])} filing(s) could not be read")
    if payload.get("pending"):
        problems.append(f"{payload['pending']} filing(s) not read yet (read over the next passes)")
    if payload.get("no_xbrl"):
        problems.append(f"{payload['no_xbrl']} filing(s) without an XBRL file")
    if win[0] and win[0] > start.isoformat():
        problems.append(f"filings read from {win[0]} only")
    net = net_insider(rows, today, days)
    if net.missing_value:
        problems.append(f"{net.missing_value} counted trade(s) filed without a value (not in the totals)")
    complete = _ok(st) and not problems
    from finresearch.fincalc.disclosures import classify, trade_day

    trades = []
    for t in sorted(rows, key=lambda x: str(trade_day(x) or ""), reverse=True):
        d = trade_day(t)
        if d is None or d < start:
            continue
        side, why = classify(t)
        trades.append({"day": _iso(d), "person": t.get("person"), "category": t.get("category"),
                       "mode": t.get("mode"), "side": t.get("side"), "quantity": t.get("quantity"),
                       "value_inr": t.get("value_inr"), "counted": side is not None, "excluded_why": why,
                       "instrument": t.get("instrument"), "filing_url": t.get("filing_url")})  # fmt: skip
    return {**st, "complete": complete, "problems": problems, "window": [_iso(start), _iso(today)],
            "net": net.json() if complete else None, "partial_net": None if complete else net.json(),
            "trades": trades[:60],
            "basis": "Open-market purchases minus sales of equity by insiders (SEBI PIT Regulation 7(2) filings), by "
                     "trade date, in ₹ as filed. ESOP allotments, gifts, inter-se and off-market transfers, pledges "
                     "and other non-market modes are excluded and counted. Context, not a signal."}  # fmt: skip


def sast(s: Session, symbol: str, now: datetime, days: int = 365) -> dict[str, Any]:
    today = to_ist(now).date()
    st = feed_state(store.feed(s, "sast", symbol), now, "sast")
    rows = [r.data for r in store.records(s, "sast", symbol=symbol, since=today - timedelta(days=days))]
    return {**st, "rows": rows[:30]}


def deals(s: Session, symbol: str, now: datetime, days: int = 90) -> dict[str, Any]:
    today = to_ist(now).date()
    st = feed_state(store.feed(s, "deals", symbol), now, "deals")
    rows = [r.data for r in store.records(s, "deal", symbol=symbol, since=today - timedelta(days=days - 1))]
    recent = [
        r
        for r in rows
        if r.get("day") and r["day"] >= (today - timedelta(days=DEAL_DAYS_RECENT - 1)).isoformat()
    ]
    return {**st, "rows": rows[:60], "recent_n": len(recent), "recent_days": DEAL_DAYS_RECENT}


# --------------------------------------------------------------------------- ratings and SEBI orders
def ratings(s: Session, now: datetime, *, isin: str | None, days: int = RATING_DAYS) -> dict[str, Any]:
    """Rating filings for this instrument (same ISIN) and its issuer's other instruments (same ISIN issuer code)."""
    st = feed_state(store.feed(s, "credit_ratings"), now, "credit_ratings")
    code = issuer_code(isin)
    since = to_ist(now).date() - timedelta(days=days)
    rows = []
    for r in store.records(s, "rating", issuer=code, since=since) if code else []:
        rows.append({**r.data, "scope": "this instrument" if r.isin == isin else "same issuer"})
    adverse = [r for r in rows if r.get("adverse")]
    return {**st, "issuer_code": code, "actions": rows[:50], "latest_adverse": adverse[0] if adverse else None,
            "basis": "Credit-rating filings by listed issuers on NSE (agency, rating, action, outlook). The action is "
                     "the filing's own verb; a bare reaffirm/other is refined only against the same agency's earlier "
                     "rating. Matched by ISIN issuer code (characters 1-7)."}  # fmt: skip


def sebi_orders(s: Session, key: str, now: datetime, days: int = 365) -> dict[str, Any]:
    st = feed_state(store.feed(s, "sebi_orders"), now, "sebi_orders")
    rows = [
        r.data
        for r in store.records(s, "sebi_order", symbol=key, since=to_ist(now).date() - timedelta(days=days))
    ]
    return {**st, "orders": rows[:20],
            "basis": "SEBI enforcement orders from SEBI's RSS whose title names this company's legal name (exact, "
                     "normalised): a POSSIBLE match, open the order to confirm. Only matched orders are kept, with "
                     "PANs removed; the feed holds SEBI's latest ~30 items, so older orders are not covered."}  # fmt: skip


# --------------------------------------------------------------------------- everything for one stock
def flags_of(symbol: str, surv: dict[str, Any], pl: dict[str, Any]) -> list[dict[str, Any]]:
    """Badges: only what is present (positive flags), each with its source and as-of."""
    out = []
    for ds in ("asm", "gsm"):
        for e in surv[ds]["entries"]:
            tone = "loss" if ds == "gsm" or e.get("ibc") else "warn"
            out.append({"kind": ds, "label": surv_label(e), "tone": tone, "as_of": surv[ds]["as_of"],
                        "source_url": surv[ds]["source_url"], "code": e.get("code"),
                        "stale": not _ok(surv[ds])})  # fmt: skip
    if surv["fno_ban"].get("in_ban"):
        out.append({"kind": "fno_ban", "label": f"F&O ban ({surv['fno_ban']['trade_date']})", "tone": "warn",
                    "as_of": surv["fno_ban"]["trade_date"], "source_url": surv["fno_ban"]["source_url"]})  # fmt: skip
    latest = pl.get("latest") or {}
    pct = _dec(latest.get("pct_of_promoter"))
    if pct is not None and pct > 0:
        ch = _dec(pl.get("change_pp"))
        rising = ch is not None and ch > 0
        text = f"Promoter pledge {pct:.2f}% of holding" + (f" (+{ch:.2f} pp q/q)" if rising else "")
        out.append({"kind": "pledge", "label": text, "tone": "warn" if rising else "info",
                    "as_of": latest.get("quarter_end"), "source_url": pl.get("source_url"),
                    "stale": not _ok(pl)})  # fmt: skip
    return out


def stock(s: Session, symbol: str, now: datetime, isin: str | None = None) -> dict[str, Any]:
    symbol = symbol.upper()
    if symbol.startswith("BSE:"):
        note = "BSE-only stock: NSE's surveillance, pledge, insider and deal feeds do not list it."
        return {"symbol": symbol, "covered": False, "note": note}
    isin = resolve_isin(s, symbol, isin)
    surv = surveillance(s, symbol, now, isin)
    pl = pledge(s, symbol, now)
    parts = {"surveillance": surv, "pledge": pl, "insider": insider(s, symbol, now), "sast": sast(s, symbol, now),
             "deals": deals(s, symbol, now), "ratings": ratings(s, now, isin=isin),
             "sebi": sebi_orders(s, symbol, now)}  # fmt: skip
    unavailable = [LABEL[k] for k in ("asm", "gsm", "fno_ban") if not _ok(surv[k])]
    unavailable += [LABEL[k] for k, v in (("pledge", pl), ("pit", parts["insider"]), ("sast", parts["sast"]),
                    ("deals", parts["deals"]), ("credit_ratings", parts["ratings"]),
                    ("sebi_orders", parts["sebi"])) if not _ok(v)]  # fmt: skip
    return {
        "symbol": symbol,
        "isin": isin,
        "covered": True,
        "as_of": to_ist(now).isoformat(),
        "flags": flags_of(symbol, surv, pl),
        "stage": stage_text(surv),
        "unavailable": unavailable,
        **parts,
    }


def bond(s: Session, isin: str, now: datetime) -> dict[str, Any]:
    isin = isin.upper()
    r = ratings(s, now, isin=isin)
    return {"isin": isin, "ratings": r, "sebi": sebi_orders(s, isin, now)}


# --------------------------------------------------------------------------- tracked overview and the brief
def tracked_overview(s: Session, now: datetime) -> dict[str, Any]:
    """Flags for every held or watched stock and every tracked bond (database only)."""
    t = store.tracked(s)
    stocks = []
    for sym, v in sorted(t.stocks.items()):
        surv = surveillance(s, sym, now, v.get("isin"))
        pl = pledge(s, sym, now)
        ins = insider(s, sym, now)
        dl = deals(s, sym, now)
        rt = ratings(s, now, isin=v.get("isin"))
        sb = sebi_orders(s, sym, now, days=30)
        states = {"asm": surv["asm"]["state"], "gsm": surv["gsm"]["state"], "fno_ban": surv["fno_ban"]["state"],
                  "pledge": pl["state"], "pit": ins["state"], "deals": dl["state"]}  # fmt: skip
        stocks.append({"key": sym, "name": v.get("name"), "isin": v.get("isin"), "held": v["held"],
                       "watched": v["watched"], "flags": flags_of(sym, surv, pl), "states": states,
                       "insider_net_90d": (ins["net"] or {}).get("net_value"), "insider_complete": ins["complete"],
                       "deals_recent": dl["recent_n"], "rating_adverse": rt["latest_adverse"],
                       "sebi_orders": sb["orders"][:3]})  # fmt: skip
    bonds = []
    for isin, v in sorted(t.bonds.items()):
        rt = ratings(s, now, isin=isin)
        bonds.append({"isin": isin, "name": v.get("name"), "held": v["held"], "tracked": v["tracked"],
                      "rating_state": rt["state"], "latest": rt["actions"][0] if rt["actions"] else None,
                      "latest_adverse": rt["latest_adverse"]})  # fmt: skip
    market = {ds: feed_state(store.feed(s, ds), now, ds) for ds in ("asm", "gsm", "fno_ban", "credit_ratings",
                                                                     "sebi_orders")}  # fmt: skip
    return {
        "as_of": to_ist(now).isoformat(),
        "stocks": stocks,
        "bonds": bonds,
        "market": market,
        "bse_only": sorted(t.bse_only),
        "unavailable": [LABEL[k] for k, v in market.items() if not _ok(v)],
    }


def brief_section(s: Session, now: datetime) -> dict[str, Any]:
    """The morning brief's "Red flags and disclosures" block for holdings and the watchlist."""
    today = to_ist(now).date()
    ov = tracked_overview(s, now)
    flags, insiders, deal_rows, rating_rows, orders = [], [], [], [], []
    for x in ov["stocks"]:
        who = {"key": x["key"], "name": x["name"], "held": x["held"], "watched": x["watched"]}
        for f in x["flags"]:
            flags.append({**who, **f})
        if x["insider_complete"] and x["insider_net_90d"] not in (None, "0"):
            insiders.append({**who, "net_value": x["insider_net_90d"]})
        if x["deals_recent"]:
            for d in deals(s, x["key"], now, days=DEAL_DAYS_RECENT)["rows"]:
                deal_rows.append(
                    {**who, **{k: d.get(k) for k in ("kind", "day", "side", "client", "quantity", "price")}}
                )
        rt = ratings(s, now, isin=x["isin"], days=NEW_ITEM_DAYS)
        rating_rows += [
            {**who, **a} for a in rt["actions"] if a.get("adverse") or a.get("action") in ("upgrade",)
        ]
        orders += [
            {**who, **o}
            for o in x["sebi_orders"]
            if o.get("day") and o["day"] >= (today - timedelta(days=14)).isoformat()
        ]
    for b in ov["bonds"]:
        rt = ratings(s, now, isin=b["isin"], days=NEW_ITEM_DAYS)
        rating_rows += [{"key": b["isin"], "name": b["name"], "held": b["held"], "watched": b["tracked"], **a}
                        for a in rt["actions"] if a.get("adverse") or a.get("action") == "upgrade"]  # fmt: skip
    stock_unavail = sorted({LABEL[k] for x in ov["stocks"] for k, st in x["states"].items() if st != "ok"})
    return {"flags": flags, "insider": insiders, "deals": deal_rows, "ratings": rating_rows, "sebi_orders": orders,
            "unavailable": sorted(set(ov["unavailable"]) | set(stock_unavail)), "tracked": len(ov["stocks"]),
            "bonds": len(ov["bonds"]), "bse_only": ov["bse_only"]}  # fmt: skip


def _dec(x: Any) -> Decimal | None:
    if x is None or x == "":
        return None
    try:
        return Decimal(str(x))
    except Exception:
        return None
