"""Fund category rank and percentile, computed locally from AMFI NAVs (research item #13, issue #177).

The arithmetic is in fincalc.fund_rank; this module picks the universe, gathers the data politely and keeps the
result on disk.

Universe. Every open-ended scheme in AMFI's NAVAll, grouped by its category heading with spelling variants folded
(adapters.amfi.category_key). Direct plans, growth options only (no IDCW, bonus or segregated-portfolio rows), one
row per scheme (deduplicated by scheme name and growth ISIN). A fund must have a NAV at the as-of month-end. Not
ranked, with the reason shown: index funds, ETFs and funds of funds (a Nifty 50 fund and a gilt index fund share
"Index Funds"; compare passive funds by cost and tracking error), sectoral and thematic funds (a banking fund and a
pharma fund are not peers), AMFI's legacy headings without a SEBI category ("Income", "Growth"), and close-ended or
interval schemes.

Data, and why it is polite. One AMFI all-scheme NAV snapshot per month-end over 5 years (61 requests the first
time; signals.fund._snapshot keeps every snapshot older than 5 days on disk for good and shares the quarter-end
ones with the fund signal), so afterwards each month adds one request. Plus NAVAll and the monthly TER file (one
request each, both cached). This runs in the monitor once a day (monitor.fund_ranks); a page load only reads the
stored result (`load`), never fetches histories.

As-of date. The latest month-end on or before yesterday (IST), the same date for every fund, like a factsheet's.
AMFI publishes a day's NAVs by that night, so yesterday's month-end is complete.

Survivorship. NAVAll lists only schemes alive today: merged or closed schemes vanish, so ranks are among the
survivors, and survivors tend to be the better performers (the median is flattered).
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from finresearch.adapters.amfi import category_key, ter_key
from finresearch.fincalc import fund_rank as R
from finresearch.fincalc.dates import today_ist

NAV_ALL_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
TER_PAGE = "https://www.amfiindia.com/ter-of-mf-schemes"
SEBI_CATEGORIES = "https://www.sebi.gov.in/legal/circulars/oct-2017/categorization-and-rationalization-of-mutual-fund-schemes_36199.html"

HISTORY_MONTHS = 60  # 5 years of month-ends (the 5-year CAGR's start)
NEAR_DAYS = 10  # a NAV further than this before a grid date means the scheme was not priced then
STORE_VERSION = 1
NOT_COMPUTED = (
    "Category ranks have not been computed yet. The monitor computes them once a day (from 07:00 IST); "
    "`finresearch fund rank` computes them now (about a minute the first time)."
)

UNRANKED_GROUPS = {
    "etf": "exchange-traded funds: compare them by cost and tracking error against the same index",
    "index": "index funds track different indices: compare them by cost and tracking error",
    "fof": "funds of funds hold other funds with different mandates",
    "other": "index funds, ETFs and funds of funds: compare passive funds by cost and tracking error",
    "legacy": "an old AMFI heading, not a SEBI category",
}
UNRANKED_NAMES = {
    "sectoral": "sectoral and thematic funds bet on different sectors or themes: they are not peers",
    "thematic": "sectoral and thematic funds bet on different sectors or themes: they are not peers",
    "sectoral/ thematic": "sectoral and thematic funds bet on different sectors or themes: they are not peers",
}

CAVEATS = [
    "Past returns. These ranks describe what already happened and say little about what comes next: performance "
    "persistence among funds is weak (Carhart 1997; SPIVA India).",
    "Survivorship: AMFI's NAV file lists only schemes alive today. Merged or closed schemes vanish, so these are "
    "ranks among survivors, whose median is flattered.",
    "Month-end NAVs: the drawdown misses falls that recovered within a month, and the Sortino ratio rests on 36 "
    "monthly returns.",
    "Category headings are AMFI's. Where AMCs use differently named headings (for example 'Short Duration' and "
    "'Short Term') the funds are ranked separately until it is clear the two are the same SEBI category.",
]


# --------------------------------------------------------------------------- data sources (overridable in tests)
@dataclass
class RankSources:
    schemes: Callable[[], Awaitable[list]] | None = None  # () -> NAVAll rows (SchemeNav)
    snapshot: Callable[[date], Awaitable[dict]] | None = None  # anchor -> {code: (NAV date, NAV)}
    ter: Callable[[date], Awaitable[dict]] | None = None  # month -> {ter_key(name): SchemeTer}
    today: Callable[[], date] = today_ist


SOURCES = RankSources()


async def _schemes() -> list:
    from finresearch.signals import fund

    return await (SOURCES.schemes or fund._schemes)()


async def _snapshot(anchor: date) -> dict:
    from finresearch.signals import fund

    return await (SOURCES.snapshot or fund._snapshot)(anchor)


async def _ter(month: date) -> dict:
    from finresearch.signals import fund

    return await (SOURCES.ter or fund._ter)(month)


# --------------------------------------------------------------------------- universe
def unranked_reason(key: str | None, structure: str | None) -> str | None:
    if key is None:
        return "no AMFI category"
    if structure in ("close", "interval"):
        return f"{structure}-ended schemes are not ranked"
    group, _, name = key.partition(":")
    return UNRANKED_GROUPS.get(group) or UNRANKED_NAMES.get(name)


def base_name(x) -> str:
    """The scheme's name without plan/option words (old NAVAll rows put them in the name: "X Fund - Direct Plan -
    Growth"), as a join key."""
    name = x.name if x.plan else re.split(r"\s*-\s*(?:direct|regular)\b", x.name, maxsplit=1, flags=re.I)[0]
    return ter_key(name)


# a segregated portfolio's own row ("UTI - Credit Risk Fund (Segregated - 06032020)", "X Fund (Segregated Portfolio
# 1)"), not the main scheme that mentions how many it has ("(No. of segregated portfolios- 3)")
SEGREGATED = re.compile(r"segregated\s*(?:-|portfolio\s*\d)", re.I)


def is_direct_growth(x) -> bool:
    """Direct plan, growth option; never IDCW/dividend, bonus or a segregated portfolio."""
    from finresearch.signals.fund import is_direct, is_growth

    text = f"{x.name} {x.option or ''}".lower()
    return is_direct(x) and is_growth(x) and "bonus" not in text and not SEGREGATED.search(x.name)


def universe(rows: list) -> tuple[dict[str, list], dict[str, str]]:
    """({category key: one direct-growth row per scheme}, {any scheme code: the code that represents its scheme in
    the ranking}). The second maps regular plans and IDCW options to their scheme's direct-growth row."""
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for x in rows:
        key = category_key(x.category)
        if key and x.structure not in ("close", "interval"):
            groups[(key, base_name(x))].append(x)
    picked: dict[str, list] = defaultdict(list)
    alias: dict[str, str] = {}
    by_isin: dict[str, str] = {}
    for (key, _), members in sorted(groups.items()):
        dg = [x for x in members if is_direct_growth(x) and x.nav is not None and x.day is not None]
        if not dg:
            continue
        # duplicates of one scheme's direct-growth row: the one priced most recently, then the oldest code
        dg.sort(key=lambda x: (-x.day.toordinal(), int(x.code) if x.code.isdigit() else 0))
        best = dg[0]
        rep = by_isin.get(best.isin_growth or "")  # the same growth ISIN under another name: one scheme
        if rep is None:
            picked[key].append(best)
            rep = best.code
            if best.isin_growth:
                by_isin[best.isin_growth] = rep
        for x in members:
            alias[x.code] = rep
    return picked, alias


def _near(snap: dict, code: str, anchor: date) -> R.Point:
    hit = snap.get(code)
    return hit if hit and 0 <= (anchor - hit[0]).days <= NEAR_DAYS else None


def _ter_of(x, table: dict) -> float | None:
    t = table.get(ter_key(x.name)) or table.get(base_name(x))
    return float(t.direct) if t is not None and t.direct is not None else None


def _labels(rows: list) -> dict[str, tuple[str, list[str]]]:
    """category key -> (display label, raw headings), the label from the most common heading."""
    raw: dict[str, Counter] = defaultdict(Counter)
    for x in rows:
        if (k := category_key(x.category)) is not None:
            raw[k][x.category] += 1
    out = {}
    for k, c in raw.items():
        top = c.most_common(1)[0][0]
        out[k] = (top.split(" - ", 1)[-1].replace("**", "").strip(), sorted(c))
    return out


# --------------------------------------------------------------------------- compute and store
def as_of_for(today: date) -> date:
    """The latest month-end on or before yesterday: AMFI has every NAV of that day by now."""
    return R.latest_month_end(today - timedelta(days=1))


async def compute(today: date | None = None, mar_annual: Decimal = R.MAR_DEFAULT) -> dict[str, Any]:
    """Rank every rankable category (see the module docstring). Reads 61 month-end snapshots (cached on disk once
    older than 5 days), NAVAll and the TER file."""
    today = today or SOURCES.today()
    rows = await _schemes()
    as_of = as_of_for(today)
    grid = R.month_grid(as_of, HISTORY_MONTHS)
    snaps = [await _snapshot(d) for d in grid]  # one after another: AMFI is asked at the polite client's pace
    ter_error = None
    try:
        table = await _ter(today)
    except Exception as e:  # TER is one column: the rest still ranks, and the payload says why TER is missing
        table, ter_error = {}, f"AMFI TER file unavailable: {type(e).__name__}: {e}"[:300]
    ter_day = max((t.day for t in table.values()), default=None)
    picked, alias = universe(rows)
    labels = _labels(rows)
    categories: dict[str, Any] = {}
    unranked: dict[str, Any] = {}
    for key, members in sorted(picked.items()):
        label, raw = labels.get(key, (key, []))
        why = unranked_reason(key, "open")
        if why:
            unranked[key] = {"label": label, "raw_labels": raw, "reason": why, "schemes": len(members)}
            continue
        inputs, excluded = [], []
        for x in sorted(members, key=lambda x: x.name.lower()):
            pts = [_near(s, x.code, d) for s, d in zip(snaps, grid, strict=True)]
            if pts[0] is None:
                launched = x.day and x.day > as_of
                excluded.append({"code": x.code, "name": x.name, "reason": "no NAV at the as-of month-end" + (
                    " (launched after it)" if launched else " (not priced then)")})  # fmt: skip
                continue
            inputs.append(R.RankInput(x.code, x.name, pts, _ter_of(x, table), x.amc))
        res = R.rank_category(inputs, mar_annual)
        categories[key] = {"key": key, "label": label, "raw_labels": raw, **res, "excluded": excluded}
    return {"version": STORE_VERSION, "as_of": as_of.isoformat(), "generated_at": datetime.now(UTC).isoformat(),
            "grid": [d.isoformat() for d in grid], "sampling": "month-end NAVs", "mar": float(mar_annual),
            "ter_day": ter_day.isoformat() if ter_day else None, "ter_error": ter_error,
            "metrics": [{"key": m.key, "label": m.label, "higher_is_better": m.higher_is_better, "unit": m.unit}
                        for m in R.METRICS],
            "categories": categories, "unranked": unranked,
            "labels": {k: v[0] for k, v in labels.items()},
            "index": {code: [category_key(x.category), alias.get(code)] for x in rows
                      if (code := x.code) and x.structure not in ("close", "interval")},
            "caveats": CAVEATS, "method": R.__doc__,
            "sources": [NAV_ALL_URL, HISTORY_URL, TER_PAGE, SEBI_CATEGORIES]}  # fmt: skip


def store_path() -> Path:
    from finresearch.config import get_settings

    return get_settings().state_dir / "fund_ranks" / "latest.json"


def save(result: dict[str, Any]) -> Path:
    """Write atomically (a temp file renamed over the old one), so the API never reads half a file."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(result, separators=(",", ":")))
    os.replace(tmp, path)
    return path


_LOADED: dict[str, Any] = {}


def load() -> dict[str, Any] | None:
    """The stored ranking, re-read only when the file changed; None before the first run."""
    path = store_path()
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    if _LOADED.get("path") != str(path) or _LOADED.get("mtime") != mtime:
        _LOADED.update(path=str(path), mtime=mtime, data=json.loads(path.read_text()))
    return _LOADED["data"]


async def refresh(today: date | None = None) -> dict[str, Any]:
    """Compute and store; returns a short summary."""
    res = await compute(today)
    save(res)
    return {"as_of": res["as_of"], "categories": len(res["categories"]),
            "funds": sum(c["size"] for c in res["categories"].values())}  # fmt: skip


# --------------------------------------------------------------------------- reading one fund
def lookup(data: dict[str, Any], code: str) -> dict[str, Any]:
    """Where a scheme sits: {"status": "ok" | "not_ranked" | "excluded" | "unknown", ...}. A regular plan or IDCW
    option is ranked through its scheme's direct-growth row (`via`)."""
    entry = data.get("index", {}).get(code)
    if entry is None:
        return {
            "status": "unknown",
            "reason": "not an open-ended scheme in AMFI's NAV file on the last ranking run",
        }
    key, ranked_code = entry
    label = data.get("labels", {}).get(key)
    why = unranked_reason(key, "open")
    if why:
        return {"status": "not_ranked", "category_key": key, "label": label, "reason": why}
    cat = data.get("categories", {}).get(key)
    if cat is None or ranked_code is None:
        return {"status": "not_ranked", "category_key": key, "label": label,
                "reason": "no direct-plan growth option of this scheme is priced in AMFI's NAV file"}  # fmt: skip
    via = ranked_code if ranked_code != code else None
    row = next((f for f in cat["funds"] if f["code"] == ranked_code), None)
    if row is None:
        ex = next((f for f in cat["excluded"] if f["code"] == ranked_code), None)
        return {"status": "excluded", "category_key": key, "label": cat["label"], "via": via,
                "reason": ex["reason"] if ex else "not in the category on the last ranking run"}  # fmt: skip
    return {"status": "ok", "category_key": key, "label": cat["label"], "via": via, "fund": row,
            "size": cat["size"], "counts": cat["counts"]}  # fmt: skip


def summary(data: dict[str, Any], code: str, metric: str = "cagr_3y") -> dict[str, Any]:
    """'12 / 34 in Flexi Cap, 3y' for one scheme and one metric."""
    hit = lookup(data, code)
    out = {"code": code, "status": hit["status"], "metric": metric, "as_of": data.get("as_of"),
           "label": hit.get("label"), "via": hit.get("via"), "reason": hit.get("reason"), "rank": None, "of": None,
           "percentile": None, "value": None, "category_size": None}  # fmt: skip
    if hit["status"] == "ok":
        f = hit["fund"]
        out |= {"rank": f["ranks"].get(metric), "of": hit["counts"].get(metric),
                "percentile": f["percentiles"].get(metric), "value": f["values"].get(metric),
                "category_size": hit["size"]}  # fmt: skip
        if out["rank"] is None:
            out["reason"] = f["missing"].get(metric) or "too few funds in the category have this metric"
    return out
