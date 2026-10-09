"""End of life of tracked instruments (#293): a bond that matured or left NSE's list, a fund AMFI no longer lists, a
stock no longer in NSE's or BSE's equity list. An ended instrument is no longer fetched (disclosures, alert targets,
the IV / intraday / sector-P/E archives, the bond list); its research packs stay readable.

The rule is deliberately slow: one missing day is a glitch (a partial download, a list NSE published late), so an
instrument ends only after END_AFTER_PASSES consecutive passes in which a complete source list did not have it, and
a source that failed or came back implausibly short is no pass at all. A bond also ends on its maturity date. Unknown
stays unknown: an instrument never seen in any pass is not ended, and a bond with no known maturity is not matured.

State is one `disclosure_feed` row per instrument (dataset "lifecycle", key "<kind>:<id>": "bond:<ISIN>",
"fund:<AMFI code>", "stock:<NSE symbol | BSE:code>"), so held instruments without a Company row have one too. A bond's
maturity (and call date, when its research found one) is also kept in its Company.meta.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

DATASET = "lifecycle"
END_AFTER_PASSES = 10  # consecutive passes (trading days for bonds and stocks) missing from a complete source list
KEEP_MISSING = END_AFTER_PASSES  # missing days kept in the state (only the run since the last sighting matters)


def key(kind: str, ident: str) -> str:
    return f"{kind}:{ident.strip().upper()}"


def note_pass(state: dict[str, Any] | None, *, present: bool, day: date, source: str) -> dict[str, Any]:
    """The state after one pass of `source` on `day`. Present resets the missing run; missing adds `day` once (a
    retry on the same day never counts twice); END_AFTER_PASSES missing days in a row end the instrument. An ended
    instrument that reappears is live again (the end is recorded under `revived`)."""
    st = dict(state or {})
    iso = day.isoformat()
    st["checked_on"], st["source"] = iso, source
    if present:
        st["last_seen"], st["missing_days"] = iso, []
        if st.get("ended") and st["ended"].get("reason") != "matured":
            st["revived"], st["ended"] = {**st["ended"], "revived_on": iso}, None
        return st
    missing = [d for d in st.get("missing_days") or [] if d != iso] + [iso]
    st["missing_days"] = sorted(missing)[-KEEP_MISSING:]
    if not st.get("ended") and len(st["missing_days"]) >= END_AFTER_PASSES:
        st["ended"] = {"on": iso, "reason": "missing",
                       "detail": f"not in {source} for {END_AFTER_PASSES} consecutive passes "
                                 f"({st['missing_days'][0]} to {iso})"}  # fmt: skip
    return st


def matured(maturity: str | date | None, today: date) -> bool:
    """True only when the maturity is known and has come; an unknown maturity is never "matured"."""
    if not maturity:
        return False
    d = maturity if isinstance(maturity, date) else date.fromisoformat(str(maturity)[:10])
    return d <= today


# --------------------------------------------------------------------------- storage
def states(s: Session, kind: str | None = None) -> dict[str, dict[str, Any]]:
    """{key: state} of every instrument with a lifecycle row (of one kind when given)."""
    from finresearch.db.models import DisclosureFeed

    q = select(DisclosureFeed.key, DisclosureFeed.payload).where(DisclosureFeed.dataset == DATASET)
    if kind:
        q = q.where(DisclosureFeed.key.like(f"{kind}:%"))
    return {k: dict(p or {}) for k, p in s.execute(q)}


def save(s: Session, k: str, state: dict[str, Any], now: datetime) -> None:
    from finresearch.disclosures.store import record_ok

    record_ok(s, DATASET, k, payload=state, url=None, as_of=state.get("checked_on"), now=now)


def ended(state: dict[str, Any] | None, today: date, maturity: str | date | None = None) -> dict[str, Any] | None:
    """Why an instrument has ended ({"on", "reason", "detail"}), or None while it is live or unknown."""
    st = state or {}
    mat = maturity or st.get("maturity")
    if matured(mat, today):
        return {"on": str(mat)[:10], "reason": "matured", "detail": f"matured on {str(mat)[:10]}"}
    return st.get("ended") or None


def ended_keys(s: Session, today: date) -> dict[str, dict[str, Any]]:
    """{key: why} of every ended instrument: lifecycle rows that ended, plus library bonds (Company "bond-<isin>")
    whose stored maturity has come."""
    from finresearch.db.models import Company

    out = {k: why for k, st in states(s).items() if (why := ended(st, today))}
    for slug, meta in s.execute(select(Company.slug, Company.meta).where(Company.slug.like("bond-%"))):
        k = key("bond", slug[5:])
        if k not in out and (why := ended(None, today, (meta or {}).get("maturity"))):
            out[k] = why
    return out


# --------------------------------------------------------------------------- the daily pass
# a source list shorter than this is a failed or partial download, never evidence that an instrument is gone (NSE's
# capital-market bond list has ~1,000 rows, AMFI's NAVAll ~14,000 schemes, the merged NSE+BSE equity list ~5,000+)
MIN_ROWS = {"bond": 50, "fund": 2000, "stock": 1000}
SOURCE = {"bond": "NSE's capital-market bond list", "fund": "AMFI's NAV file (NAVAll)",
          "stock": "NSE's and BSE's equity lists"}  # fmt: skip
ALERT_KIND = "instrument_ended"


def _candidates(s: Session, kind: str, live: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The tracked instruments of `kind` (live ones from tracked()) plus those that ended for being missing (so one
    that comes back is seen again)."""
    out = dict(live)
    for k, st in states(s, kind).items():
        if (st.get("ended") or {}).get("reason") == "missing":
            out.setdefault(k.split(":", 1)[1], {"name": st.get("name"), "held": False})
    return out


def _alert(s: Session, kind: str, ident: str, v: dict[str, Any], why: dict[str, Any], watch_id: int | None) -> None:
    from finresearch.db.models import Alert

    what = {"bond": "bond", "fund": "fund", "stock": "stock"}[kind]
    path = {"bond": f"/bonds/{ident}", "fund": f"/funds/{ident}", "stock": f"/stocks/{ident}"}[kind]
    held = " You still hold it: record its redemption, merger or delisting in the portfolio." if v.get("held") else ""
    s.add(Alert(watch_id=watch_id, kind=ALERT_KIND, level="warn",
                message=f"{v.get('name') or ident}: this {what} has ended ({why['detail']}); the app no longer "
                        f"fetches it. Its research stays readable.{held}",
                data={"instrument": ident, "asset": kind, "reason": why["reason"], "on": why["on"],
                      "source": SOURCE[kind], "path": path}))  # fmt: skip


def observe(s: Session, kind: str, present: set[str], rows_n: int, today: date, now: datetime,
            extra: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:  # fmt: skip
    """One pass of a source over the tracked instruments of `kind`. `present` holds the identifiers the source
    listed (ISINs, AMFI codes, stock keys and ISINs), `rows_n` its size. A short list counts as no pass at all.
    Instruments that end in this pass raise one alert each (and a stock's watches stop). Returns a summary."""
    from finresearch.disclosures.store import tracked

    if rows_n < MIN_ROWS[kind]:
        return {"skipped": f"{SOURCE[kind]} had {rows_n} rows (fewer than {MIN_ROWS[kind]}): not counted"}
    t = tracked(s, today)
    live = {"bond": t.bonds, "fund": t.funds, "stock": {**t.stocks, **t.bse_only}}[kind]
    out: dict[str, Any] = {"checked": 0, "missing": [], "ended": []}
    prior = states(s, kind)
    for ident, v in _candidates(s, kind, live).items():
        k = key(kind, ident)
        seen = ident.upper() in present or bool(v.get("isin") and v["isin"].upper() in present)
        old = prior.get(k) or {}
        st = note_pass(old, present=seen, day=today, source=SOURCE[kind])
        st["name"] = v.get("name") or old.get("name")
        st.update((extra or {}).get(ident.upper()) or {})
        save(s, k, st, now)
        out["checked"] += 1
        if not seen:
            out["missing"].append(ident)
        if st.get("ended") and not old.get("ended"):
            out["ended"].append(ident)
            _alert(s, kind, ident, v, st["ended"], _stop_watches(s, ident, st["ended"]) if kind == "stock" else None)
    s.flush()
    return out


def _stop_watches(s: Session, ident: str, why: dict[str, Any]) -> int | None:
    """Stop the active stock watches of an ended stock (no more daily checks failing on it); returns one watch id
    for the alert. The watch keeps its history and says why it stopped."""
    from finresearch.db.models import Watch
    from finresearch.monitor.watch import _cancel_pending

    first = None
    for w in s.scalars(select(Watch).where(Watch.kind == "stock", Watch.active.is_(True))):
        if (w.key or "").upper() == ident.upper():
            w.active = False
            w.meta = {**(w.meta or {}), "ended": why}
            _cancel_pending(s, w.id)
            first = first or w.id
    return first


# --------------------------------------------------------------------------- the monitor step
START = (16, 30)  # IST, trading days: after the close, before the evening disclosure pass


@dataclass
class Sources:
    """What the pass reads; tests replace these. None = the live source."""

    bonds: Callable[[], Awaitable[list[Any]]] | None = None  # -> [adapters.nse_bonds.ListedBond]
    nav_rows: Callable[[], Awaitable[list[Any]]] | None = None  # -> AMFI NAVAll rows (.code)
    listings: Callable[[], Awaitable[Any]] | None = None  # -> adapters.bse_equity.Listings


async def _live_bonds() -> list[Any]:
    from finresearch.adapters.nse_bonds import live_bonds

    return await live_bonds()


async def _live_navs() -> list[Any]:
    from finresearch.signals import fund

    return await fund._schemes()  # the monitor's NAVAll, cached 6 h in-process (shared with the fund ranking)


async def _live_listings() -> Any:
    from finresearch.monitor.portfolio_daily import _live_listings as listings

    return await listings()  # the stored ISIN map while fresh: no download most days


async def run_pass(today: date, now: datetime, src: Sources | None = None) -> dict[str, Any]:
    """Read each source the tracked instruments need (none when nothing of that kind is tracked) and record a pass.
    A source that fails is reported and counts as no pass."""
    from finresearch.db import session_scope
    from finresearch.disclosures.store import tracked
    from finresearch.orchestrator.bond import maturity_meta

    src = src or Sources()
    with session_scope() as s:
        t = tracked(s, today)
        want = {"bond": bool(t.bonds) or bool(states(s, "bond")), "fund": bool(t.funds) or bool(states(s, "fund")),
                "stock": bool(t.stocks or t.bse_only) or bool(states(s, "stock"))}  # fmt: skip
    out: dict[str, Any] = {"failed": {}}
    for kind, fetch in (("bond", src.bonds or _live_bonds), ("fund", src.nav_rows or _live_navs),
                        ("stock", src.listings or _live_listings)):  # fmt: skip
        if not want[kind]:
            continue
        try:
            got = await fetch()
        except Exception as e:
            out["failed"][kind] = f"{type(e).__name__}: {e}"[:200]
            continue
        extra: dict[str, dict[str, Any]] = {}
        if kind == "bond":
            present = {b.isin.upper() for b in got}
            extra = {b.isin.upper(): {"maturity": b.maturity.isoformat()} for b in got if b.maturity}
            n = len(got)
        elif kind == "fund":
            present, n = {str(r.code).upper() for r in got}, len(got)
        else:
            rows = list(getattr(got, "rows", []) or [])
            present = {x.upper() for r in rows for x in (r.nse_symbol, f"BSE:{r.bse_code}" if r.bse_code else None,
                                                          r.isin) if x}  # fmt: skip
            n = len(rows)
        with session_scope() as s:
            out[kind] = observe(s, kind, present, n, today, now, extra)
            if kind == "bond" and "skipped" not in out[kind]:
                _fill_bond_meta(s, extra, maturity_meta)
    return out


def _fill_bond_meta(s: Session, extra: dict[str, dict[str, Any]], maturity_meta) -> None:
    """A library bond with no stored maturity gets it from today's list (#293)."""
    from finresearch.db.models import Company

    for co in s.scalars(select(Company).where(Company.slug.like("bond-%"))):
        mat = (extra.get(co.slug[5:].upper()) or {}).get("maturity")
        if mat and not (co.meta or {}).get("maturity"):
            co.meta = {**(co.meta or {}), **maturity_meta(date.fromisoformat(mat), "NSE capital-market bond list")}


async def lifecycle_step(now: datetime, src: Sources | None = None, *,
                         holidays: set[date] | None = None) -> dict[str, Any]:  # fmt: skip
    """The monitor's daily end-of-life pass: once per NSE trading day from START (IST), claimed in alert_eval_slot
    ("lifecycle:<day>") and retried like the disclosure passes when a source failed."""
    from finresearch.fincalc.dates import to_ist
    from finresearch.monitor.disclosures import claim, finish
    from finresearch.monitor.market_days import market_day

    ist = to_ist(now)
    if (ist.hour, ist.minute) < START or not market_day(ist.date(), holidays, job="instrument lifecycle"):
        return {}
    slot = f"lifecycle:{ist.date().isoformat()}"
    if not claim(slot, now):
        return {}
    try:
        res = await run_pass(ist.date(), now, src)
    except Exception as e:
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        raise
    finish(slot, res, now, failed=bool(res["failed"]))
    return res
