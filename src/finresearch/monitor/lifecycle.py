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
