"""The monitor's weekly AMFI scheme-master check of held funds (#297).

Once per ISO week, from RUN_AFTER on the week's first day (any later tick that week if the Mac was asleep), the pass
downloads AMFI's scheme master (adapters.amfi.SCHEME_MASTER_URL) and reads NAVAll (the cached copy the fund ranking
shares) and checks every open fund holding:

* "code_vanished": its AMFI scheme code is not in the scheme master;
* "isin_vanished": its ISIN is in no scheme-master row;
* "nav_stopped": its scheme's latest NAV in NAVAll is more than STALE_NAV_DAYS older than the newest NAV day in the
  file, or the scheme has no NAVAll row (a merged, wound-up or renamed scheme, or a feed problem worth a look).

A holding with neither code nor ISIN cannot be checked and is counted as such, never as fine. Each fund raises ONE
alert ("fund_scheme_changed") when it first has an issue; it is armed again only after a check finds it clean. State
is one `disclosure_feed` row per fund (dataset "scheme_master", key "fund:<code or ISIN>"). A master or NAVAll shorter
than its MIN_ROWS is a failed download: nothing is judged. Claimed in `alert_eval_slot` ("scheme_master:<year>-W<nn>")
and retried like the other passes. The end-of-life pass (monitor.lifecycle) separately ends funds missing from NAVAll.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

WEEKDAY, WEEKDAY_NAME = 0, "Monday"  # the ISO week's first day
RUN_AFTER = time(7, 0)  # IST
STALE_NAV_DAYS = (
    7  # calendar days behind NAVAll's newest NAV day (a long weekend plus holidays is under a week)
)
MIN_ROWS = {"master": 5000, "nav": 2000}  # the master has ~16,400 rows (12-Oct-2026), NAVAll ~14,000
DATASET, ALERT_KIND = "scheme_master", "fund_scheme_changed"
REASON = {"code_vanished": "its AMFI scheme code {code} is no longer in AMFI's scheme master",
          "isin_vanished": "its ISIN {isin} is no longer in AMFI's scheme master",
          "nav_stopped": "its NAV stopped updating ({detail})"}  # fmt: skip


def due_slot(now: datetime) -> str | None:
    ist = to_ist(now)
    if ist.weekday() == WEEKDAY and ist.time() < RUN_AFTER:
        return None
    y, w, _ = ist.date().isocalendar()
    return f"scheme_master:{y}-W{w:02d}"


@dataclass(frozen=True)
class Index:
    codes: frozenset[str]
    isins: frozenset[str]
    code_by_isin: dict[str, str]
    nav_day: dict[str, date]  # code -> its latest NAV day in NAVAll
    newest: date | None  # NAVAll's newest NAV day

    @classmethod
    def build(cls, master: list[Any], navs: list[Any]) -> Index:
        nav_day: dict[str, date] = {}
        for r in navs:
            if r.day and r.nav is not None and (r.code not in nav_day or r.day > nav_day[r.code]):
                nav_day[r.code] = r.day
        return cls(frozenset(m.code for m in master), frozenset(i for m in master for i in m.isins),
                   {i: m.code for m in master for i in m.isins}, nav_day, max(nav_day.values(), default=None))  # fmt: skip


def issues_of(code: str | None, isin: str | None, idx: Index) -> list[dict[str, str]]:
    """The scheme-master and NAV issues of one fund (module docstring)."""
    out = []
    if code and code not in idx.codes:
        out.append({"kind": "code_vanished", "text": REASON["code_vanished"].format(code=code)})
    if isin and isin not in idx.isins:
        out.append({"kind": "isin_vanished", "text": REASON["isin_vanished"].format(isin=isin)})
    code = code or (idx.code_by_isin.get(isin) if isin else None)  # the code by ISIN, for the NAV check
    if code:
        mine = idx.nav_day.get(code)
        detail = None
        if mine is None:
            detail = "no NAV for it in AMFI's NAVAll"
        elif idx.newest and (idx.newest - mine).days > STALE_NAV_DAYS:
            detail = f"last NAV {mine.isoformat()}, while NAVAll has NAVs to {idx.newest.isoformat()}"
        if detail:
            out.append({"kind": "nav_stopped", "text": REASON["nav_stopped"].format(detail=detail)})
    return out


@dataclass
class Sources:
    master: Callable[[], Awaitable[list[Any]]] | None = None  # -> [SchemeMaster]
    navs: Callable[[], Awaitable[list[Any]]] | None = None  # -> NAVAll rows (SchemeNav)


async def _live_master() -> list[Any]:
    from finresearch.adapters.amfi import AmfiClient

    async with AmfiClient() as amfi:
        return await amfi.scheme_master()


async def _live_navs() -> list[Any]:
    from finresearch.signals import fund

    return await fund._schemes()


def held_funds(s: Any) -> dict[str, dict[str, Any]]:
    """{"<code or ISIN>": {"code", "isin", "name", "holdings"}} of the open fund holdings (several folios of one fund
    are one fund); holdings with neither code nor ISIN under the key "" (they cannot be checked)."""
    from sqlalchemy import func, select

    from finresearch.db.models import PortfolioHolding, PortfolioLot

    open_ids = select(PortfolioLot.holding_id).group_by(PortfolioLot.holding_id).having(
        func.sum(PortfolioLot.open_quantity) > Decimal(0))  # fmt: skip
    out: dict[str, dict[str, Any]] = {}
    for h in s.scalars(select(PortfolioHolding).where(PortfolioHolding.asset_type == "mf",
                                                      PortfolioHolding.id.in_(open_ids))):  # fmt: skip
        code, isin = (h.scheme_code or "").strip() or None, (h.isin or "").strip().upper() or None
        k = code or isin or ""
        v = out.setdefault(k, {"code": code, "isin": isin, "name": h.name, "holdings": []})
        v["isin"] = v["isin"] or isin
        v["holdings"].append(h.id)
    return out


def check(s: Any, master: list[Any], navs: list[Any], today: date, now: datetime) -> dict[str, Any]:
    """One weekly check (module docstring); returns a summary without fund names (it is logged)."""
    from finresearch.adapters.amfi import SCHEME_MASTER_URL
    from finresearch.db.models import Alert
    from finresearch.disclosures.store import record_ok

    if len(master) < MIN_ROWS["master"] or len(navs) < MIN_ROWS["nav"]:
        return {"skipped": f"scheme master {len(master)} rows, NAVAll {len(navs)} rows: a partial download, "
                           "nothing judged"}  # fmt: skip
    idx = Index.build(master, navs)
    prior = _states(s)
    funds = held_funds(s)
    out: dict[str, Any] = {"checked": 0, "unidentified": len(funds.pop("", {}).get("holdings", [])), "flagged": 0,
                           "alerted": 0}  # fmt: skip
    for k, v in funds.items():
        found = issues_of(v["code"], v["isin"], idx)
        key = f"fund:{k}"[:40]
        old = prior.get(key) or {}
        alerted = bool(old.get("alerted")) and bool(found)  # re-armed by a clean check
        if found and not old.get("alerted"):
            s.add(Alert(kind=ALERT_KIND, level="warn",
                        message=f"{v['name']}: " + "; ".join(i["text"] for i in found) + ". Check the fund house's "
                                "notices (a merger, a renamed plan or a wound-up scheme) and update the holding.",
                        data={"instrument": v["code"] or v["isin"], "asset": "fund", "issues": [i["kind"] for i in found],
                              "holdings": v["holdings"], "source": SCHEME_MASTER_URL,
                              "path": f"/funds/{v['code']}" if v["code"] else None}))  # fmt: skip
            alerted = True
            out["alerted"] += 1
        record_ok(s, DATASET, key, payload={"issues": found, "alerted": alerted, "checked_on": today.isoformat()},
                  url=SCHEME_MASTER_URL, as_of=today.isoformat(), now=now)  # fmt: skip
        out["checked"] += 1
        out["flagged"] += bool(found)
    return out


def _states(s: Any) -> dict[str, dict[str, Any]]:
    from sqlalchemy import select

    from finresearch.db.models import DisclosureFeed

    q = select(DisclosureFeed.key, DisclosureFeed.payload).where(DisclosureFeed.dataset == DATASET)
    return {k: dict(p or {}) for k, p in s.execute(q)}


async def scheme_master_step(now: datetime, src: Sources | None = None) -> dict[str, Any]:
    from finresearch.db import session_scope
    from finresearch.monitor.disclosures import claim, finish

    slot = due_slot(now)
    if slot is None or not claim(slot, now):
        return {}
    src = src or Sources()
    try:
        with session_scope() as s:
            if not held_funds(s):
                finish(slot, {"checked": 0}, now, failed=False)
                return {}
        master = await (src.master or _live_master)()
        navs = await (src.navs or _live_navs)()
        with session_scope() as s:
            res = check(s, master, navs, to_ist(now).date(), now)
    except Exception as e:
        log.warning("AMFI scheme-master check failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"scheme_master": "failed"}
    finish(slot, res, now, failed="skipped" in res)
    return {"scheme_master": res}
