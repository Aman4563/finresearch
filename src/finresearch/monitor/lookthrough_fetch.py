"""The monitor's monthly fund look-through fetch (#214): the latest month-end portfolio of every held fund whose fund
house the app can read (adapters.amc_portfolio.AMC_SOURCES, mode "auto").

When. SEBI Master Circular for Mutual Funds (as on 20-Mar-2026), para 6.1.1: portfolios as on the last day of the
month are disclosed "within 10 calendar days from the close of each month" [V: read 30-Sep-2026, page 85; the same
citation as adapters.amc_portfolio]. So the fetch runs from FIRST_DAY (the 11th, IST) at RUN_AFTER, once a day
(slot "lookthrough:<day>" in alert_eval_slot, so two monitor processes never run it twice; a run that hit a network
error is retried up to monitor.disclosures.MAX_ATTEMPTS times), until LAST_DAY. A house that publishes late is
simply tried again the next day. The target is the previous calendar month.

Cost. A fund whose newest stored portfolio is already the target month needs no request at all, so after the first
successful day the rest of the window is free; a house-wide workbook (Nippon, DSP, Tata) is downloaded once for all
its schemes; quant needs one small list request per month plus the scheme's own file.

Every held fund gets a logged reason: up to date, fetched, not published yet, paste/upload needed (Axis), unsupported
(with the house's reason, e.g. HDFC's bot protection), or an error. The result is stored in the slot row.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

FIRST_DAY = 11  # the day after SEBI's 10-calendar-day deadline
LAST_DAY = 25  # stop trying for the month after this day (a later file arrives with next month's run)
RUN_AFTER = time(8, 0)


def due_slot(now: datetime) -> str | None:
    ist = to_ist(now)
    if FIRST_DAY <= ist.day <= LAST_DAY and ist.time() >= RUN_AFTER:
        return f"lookthrough:{ist.date().isoformat()}"
    return None


PER_SCHEME = frozenset(
    {"quant", "ppfas"}
)  # houses with one file per scheme (the others: one workbook for all)


def _house_has(store: Any, amc: str, want: str) -> bool:
    """A stored file from this house already holds the wanted month (for some scheme)."""
    return any(m.get("amc") == amc and any((x.get("as_of") or "")[:7] >= want for x in m.get("schemes", []))
               for m in store.index()["files"].values())  # fmt: skip


def target_month(today: date) -> str:
    """YYYY-MM of the month-end portfolio due by now: the previous calendar month."""
    y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return f"{y:04d}-{m:02d}"


async def fetch_held(today: date, held: list[tuple[str | None, str]], *, store: Any = None, client: Any = None
                     ) -> list[dict[str, Any]]:  # fmt: skip
    """One line per held fund (code, name): what was done and why. `client`: tests pass a fake PoliteClient."""
    from finresearch.adapters.amc_portfolio import (
        AMC_SOURCES,
        AMC_UNSUPPORTED,
        UNKNOWN_HOUSE,
        AmcPortfolioError,
    )
    from finresearch.adapters.amc_portfolio import house_for as house_of
    from finresearch.portfolio import lookthrough as lt

    store = store or lt.PortfolioStore()
    want = target_month(today)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    tried: dict[str, str] = {}  # house-wide workbook already looked for in this run -> why it did not help
    for code, name in held:
        key = store.key_for(code, name)
        if not key or key in seen:
            continue
        seen.add(key)
        house = house_of(None, name)
        line: dict[str, Any] = {"code": code, "name": name, "key": key, "house": house, "target": want}
        src = AMC_SOURCES.get(house or "")
        _, newest = store.newest(key)
        have = newest.strftime("%Y-%m") if newest else None
        if src is None:
            u = AMC_UNSUPPORTED.get(house or "")
            line.update(status="unsupported", reason=f"{u.amc}: {u.reason}" if u else UNKNOWN_HOUSE)
        elif have is not None and have >= want:
            line.update(status="up_to_date", reason=f"{have} portfolio already stored")
        elif house in tried:  # Nippon/DSP/Tata: one workbook for every scheme; its page was just read
            line.update(status="not_published", reason=tried[house])
        elif src.mode != "auto":
            line.update(
                status="manual", reason=f"{src.amc}: paste the file link or upload the file ({src.note})"
            )
        else:
            try:
                await lt.fetch_scheme(store, key, src.amc, months=1, client=client, today=today)
                err = None
            except AmcPortfolioError as e:
                err = str(e)
            except Exception as e:  # network: the slot is retried
                err = f"{type(e).__name__}: {e}"
            _, newest = store.newest(key)
            got = newest.strftime("%Y-%m") if newest else None
            if got is not None and got >= want:
                line.update(status="fetched", reason=f"{got} portfolio fetched from {src.amc}")
            elif err and "no monthly portfolio file" not in err:
                line.update(status="error", reason=err[:300])
            elif house not in PER_SCHEME and _house_has(store, src.amc, want):
                line.update(status="not_matched", reason=f"{src.amc}'s {want} workbook is stored but no scheme in it "
                                                          "matches this fund's name: link it on the Look-through page")  # fmt: skip
            else:
                line.update(status="not_published", reason=f"{src.amc} has not published the {want} portfolio yet "
                                                            f"(newest stored: {got or 'none'}); tried again tomorrow")  # fmt: skip
                if house not in PER_SCHEME:
                    tried[house] = line["reason"]
        log.info("look-through fetch: %s: %s", name, line["reason"])
        out.append(line)
    return out


async def lookthrough_step(now: datetime, *, held: list[tuple[str | None, str]] | None = None, store: Any = None,
                           client: Any = None) -> dict[str, Any]:  # fmt: skip
    from finresearch.monitor.disclosures import claim, finish
    from finresearch.portfolio.lookthrough import held_mf_codes

    slot = due_slot(now)
    if slot is None or not claim(slot, now):
        return {}
    try:
        lines = await fetch_held(to_ist(now).date(), held if held is not None else held_mf_codes(), store=store,
                                 client=client)  # fmt: skip
    except Exception as e:
        log.warning("look-through fetch failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"lookthrough": "failed"}
    counts: dict[str, int] = {}
    for x in lines:
        counts[x["status"]] = counts.get(x["status"], 0) + 1
    finish(slot, {"counts": counts, "funds": lines[:60]}, now, failed=bool(counts.get("error")))
    return {"lookthrough": counts}
