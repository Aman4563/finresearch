"""NSE's daily FII derivative statistics (#371, P1.9): contracts and value (Rs crore) that foreign investors bought and
sold in each F&O category that day, and their open interest at the day's end. Read-only public file; nothing here
places or routes orders.

URL and format: `https://nsearchives.nseindia.com/content/fo/fii_stats_<DD-Mon-YYYY>.xls`, an OLE2 .xls (#371's data
check 3b confirmed the 2020 file as OLE2 .xls and a 404 for 2008; the audit also fetched 2022). The layout below is
NOT yet verified against a served file in this repo: the dataset's data-rights decision is pending
(finresearch.data_rights.PENDING), and no request is made before it is recorded. Every expectation is therefore a
named constant and anything else is refused (FiiStatsError), never read loosely: the owner's first one-day fetch
confirms or corrects it.
  - a title cell `FII DERIVATIVES STATISTICS FOR <DD-Mon-YYYY>` near the top (the date is checked against the day);
  - a header row naming BUY, SELL and OPEN INTEREST (AT THE END OF THE DAY), two columns each: No. of contracts and
    Amt in Crores;
  - one row per category (at least INDEX FUTURES, INDEX OPTIONS, STOCK FUTURES and STOCK OPTIONS; newer files may add
    per-index rows, which are kept under their own label), then free-text notes, which are skipped.

Checks: the body is a workbook (a block page is an error, never "no file"); contracts are whole and non-negative,
amounts non-negative; a category row has all six numbers or is refused; no category appears twice. A 404 is a holiday
or a day not yet published, so `fetch_day` returns None for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from finresearch.adapters import xls
from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import NSE_BASE, NseError

DATASET = "fii_stats"  # the table, the manifest, lineage and data-rights name
URL = "https://nsearchives.nseindia.com/content/fo/fii_stats_{day}.xls"
PARSER_VERSION = "1"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# unverified: the owner's first fetch confirms (module docstring)
TITLE = re.compile(r"FII\s+DERIVATIVES\s+STATISTICS\s+FOR\s+(\d{1,2})-([A-Za-z]{3})-(\d{4})", re.I)
SECTIONS = (("BUY", "buy"), ("SELL", "sell"), ("OPEN INTEREST", "oi"))  # header cell prefix -> column prefix
REQUIRED = ("INDEX FUTURES", "INDEX OPTIONS", "STOCK FUTURES", "STOCK OPTIONS")
KEYS = ("buy_contracts", "buy_amt_cr", "sell_contracts", "sell_amt_cr", "oi_contracts", "oi_amt_cr")
HEAD_ROWS = 10  # the title and header must be within the first rows


class FiiStatsError(ValueError):
    """The body is not the fii_stats workbook in the expected layout, or a value fails a check."""


@dataclass(frozen=True)
class FiiStatsDay:
    day: date
    rows: dict[str, dict[str, Any]]  # category -> KEYS (contracts int, amounts Decimal Rs crore)


def url_for(day: date) -> str:
    return URL.format(day=f"{day:%d}-{_MONTHS[day.month - 1]}-{day:%Y}")  # English month, whatever the locale


def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", v).strip().upper() if isinstance(v, str) else ""


def _num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _title_day(rows: list[list[Any]]) -> date:
    for row in rows[:HEAD_ROWS]:
        for cell in row:
            m = TITLE.search(cell) if isinstance(cell, str) else None
            if m:
                return datetime.strptime(f"{m[1]} {m[2].title()} {m[3]}", "%d %b %Y").date()
    raise FiiStatsError("no 'FII DERIVATIVES STATISTICS FOR <date>' title in the first rows")


def _columns(rows: list[list[Any]]) -> tuple[int, dict[str, int]]:
    """(index of the header row, {key: column})."""
    for i, row in enumerate(rows[:HEAD_ROWS]):
        cells = [_norm(c) for c in row]
        at = {p: next((j for j, c in enumerate(cells) if c.startswith(h)), None) for h, p in SECTIONS}
        if at["buy"] is None:
            continue
        if None in at.values():
            raise FiiStatsError(f"unexpected header: {[c for c in cells if c]}")
        sub = [_norm(c) for c in rows[i + 1]] if i + 1 < len(rows) else []
        cols: dict[str, int] = {}
        for p, j in at.items():
            got = (sub[j] if j < len(sub) else "", sub[j + 1] if j + 1 < len(sub) else "")
            if "CONTRACT" not in got[0] or "CRORE" not in got[1]:
                raise FiiStatsError(f"unexpected sub-header under {p}: {got}")
            cols[f"{p}_contracts"], cols[f"{p}_amt_cr"] = j, j + 1
        return i, cols
    raise FiiStatsError("no BUY / SELL / OPEN INTEREST header in the first rows")


def parse(content: bytes) -> FiiStatsDay:
    """Every category's six numbers; raises FiiStatsError when the layout or a check fails."""
    try:
        sheets = xls.read_sheets(content)
    except xls.NotAWorkbook as e:
        raise FiiStatsError(f"not a workbook: {e}") from e
    rows = next(iter(sheets.values()), [])
    day = _title_day(rows)
    head, cols = _columns(rows)
    out: dict[str, dict[str, Any]] = {}
    for row in rows[head + 2 :]:
        label = _norm(row[0]) if row else ""
        vals = [row[c] if c < len(row) else None for c in cols.values()]
        if not label or not any(_num(v) for v in vals):
            continue  # a blank row or a note
        if not all(_num(v) for v in vals):
            raise FiiStatsError(f"row {label!r}: not all six numbers are present")
        if label in out:
            raise FiiStatsError(f"row {label!r} appears twice")
        rec: dict[str, Any] = {}
        for k, v in zip(cols, vals, strict=True):
            if v < 0:
                raise FiiStatsError(f"row {label!r}: {k} is negative ({v})")
            if k.endswith("_contracts"):
                if v != int(v):
                    raise FiiStatsError(f"row {label!r}: {k} is not a whole number ({v})")
                rec[k] = int(v)
            else:
                rec[k] = Decimal(str(v))
        out[label] = {k: rec[k] for k in KEYS}
    missing = [c for c in REQUIRED if c not in out]
    if missing:
        raise FiiStatsError(f"missing rows: {missing}")
    return FiiStatsDay(day, out)


async def fetch_day(http: PoliteClient, day: date) -> tuple[str, bytes] | None:
    """(url, body as served) of the day's workbook; None on HTTP 404 (holiday, weekend or not yet published). A
    refusal, a 5xx or a body that is not a workbook raises NseError: never read as "no file". GET only, through the
    shared NSE budget."""
    url = url_for(day)
    resp = await http.get(url, headers={"Referer": f"{NSE_BASE}/", "Accept": "application/vnd.ms-excel,*/*"})
    if resp.status == 404:
        return None
    if not resp.ok:
        raise NseError(f"NSE HTTP {resp.status} for {url}")
    if xls.kind(resp.content) is None:
        raise NseError(f"NSE served something else (HTTP {resp.status}) for {url}: a block page?")
    return url, resp.content
