"""Fund look-through: the on-disk store of AMC monthly portfolios, AMC downloads, and the portfolio-level views.

Storage (no database tables): under <portfolio_dir>/lookthrough/ (data/portfolio/lookthrough/, gitignored):
- files/<sha16>.<ext>  the file exactly as downloaded or uploaded;
- parsed/<sha16>.json  every scheme portfolio parsed from it (one Nippon workbook holds ~100 schemes);
- index.json           file metadata (URL, AMC, fetched at, schemes and months inside) and manual scheme links;
- amfi_cap_list.json   AMFI's large/mid/small list by ISIN.
The AMC files are public; which of them sit here says which funds the user looked at, so they live with the rest
of the personal portfolio data and are never sent to an LLM.

`lookthrough_exposure(session, prices, today)` is the entry point other packages use (WP-A concentration, WP-D
alerts): it returns the portfolio's look-through exposure as plain JSON-ready data.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from finresearch.adapters.amc_portfolio import (
    AMC_SOURCES,
    AMC_UNSUPPORTED,
    AMFI_CAP_LIST_URLS,
    QUANT_LIST_URL,
    UNKNOWN_HOUSE,
    AmcPortfolioError,
    SchemePortfolio,
    check_url,
    discover_links,
    fetched_at_now,
    house_for,
    parse_amfi_cap_list,
    parse_file,
    pick_links,
    quant_list_body,
    scheme_key,
    source_for_amc,
)
from finresearch.fincalc.lookthrough import (
    BUCKET_LABEL,
    EQUITY_KINDS,
    DirectInput,
    FundInput,
    FundLine,
    active_share,
    cap_label,
    combine,
    hhi,
    lookthrough,
    overlap,
    style_series,
)

ZERO = Decimal(0)
PARSER_VERSION = 3  # bump when parsing changes: stored files are re-parsed from the kept originals
STALE_DAYS = (
    45  # a month-end portfolio older than this is flagged (SEBI: published within 10 days of month-end)
)
CAP_LIST_MAX_AGE_DAYS = 30  # re-check AMFI for a newer list (Jan/Jul) at most monthly
DISCLAIMER = ("Personal, non-advisory arithmetic on the fund houses' published month-end portfolios. Not a "
              "recommendation to buy, sell or switch any fund.")  # fmt: skip
LIMITS = [
    "Month-end snapshots published up to 10 days later: today's portfolio can differ (and window dressing exists).",
    "Hedged arbitrage positions are excluded from equity only where the fund house labels them (PPFAS does); "
    "elsewhere a hedged long counts as equity.",
    "Units of other mutual funds (fund-of-funds, liquid-fund parking) are not looked through.",
    "Market-cap buckets use AMFI's list by ISIN; stocks not on it (new listings, foreign shares) are shown apart.",
    "Sector names follow the fund houses' industry labels; a direct stock not held by any fund keeps its NSE label.",
]


def _f(x: Decimal | None, nd: int = 2) -> float | None:
    return None if x is None else round(float(x), nd)


def default_root() -> Path:
    from finresearch.config import get_settings

    return get_settings().portfolio_dir / "lookthrough"


# ----------------------------------------------------------------------------------------------- store
@dataclass(frozen=True)
class Found:
    sha: str
    portfolio: SchemePortfolio
    meta: dict[str, Any]

    @property
    def month(self) -> str:
        return self.portfolio.month or "unknown"


class PortfolioStore:
    """AMC files and their parsed portfolios on disk; every read goes through the index."""

    _lock = threading.Lock()

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else default_root()
        self._parsed: dict[str, list[SchemePortfolio]] = {}

    # -- index
    def _index_path(self) -> Path:
        return self.root / "index.json"

    def index(self) -> dict[str, Any]:
        p = self._index_path()
        if not p.exists():
            return {"files": {}, "aliases": {}}
        data = json.loads(p.read_text())
        data.setdefault("files", {})
        data.setdefault("aliases", {})
        return data

    def _write_index(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._index_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        tmp.replace(self._index_path())

    # -- files
    def add_file(self, data: bytes, filename: str, *, url: str | None = None, amc: str | None = None
                 ) -> tuple[str, list[SchemePortfolio]]:  # fmt: skip
        """Parse and keep a file (idempotent by content hash). Raises AmcPortfolioError for unreadable files."""
        portfolios = parse_file(data, filename)
        sha = hashlib.sha256(data).hexdigest()[:16]
        ext = (Path(filename).suffix or ".xlsx").lower()[:6]
        with self._lock:
            (self.root / "files").mkdir(parents=True, exist_ok=True)
            (self.root / "parsed").mkdir(parents=True, exist_ok=True)
            (self.root / "files" / f"{sha}{ext}").write_bytes(data)
            self._write_parsed(sha, portfolios)
            idx = self.index()
            idx["files"][sha] = {
                "filename": Path(filename).name[:200], "url": url, "amc": amc, "fetched_at": fetched_at_now(),
                "size": len(data),
                "schemes": [{"sheet": p.sheet, "name": p.scheme_name, "key": p.key,
                             "as_of": p.as_of.isoformat() if p.as_of else None, "benchmark": p.benchmark,
                             "holdings": len(p.holdings), "warnings": p.warnings} for p in portfolios],
            }  # fmt: skip
            self._write_index(idx)
        self._parsed[sha] = portfolios
        return sha, portfolios

    def delete_file(self, sha: str) -> bool:
        with self._lock:
            idx = self.index()
            meta = idx["files"].pop(sha, None)
            if meta is None:
                return False
            for p in (self.root / "files").glob(f"{sha}.*"):
                p.unlink(missing_ok=True)
            (self.root / "parsed" / f"{sha}.json").unlink(missing_ok=True)
            self._write_index(idx)
        self._parsed.pop(sha, None)
        return True

    def _write_parsed(self, sha: str, portfolios: list[SchemePortfolio]) -> None:
        body = {"parser": PARSER_VERSION, "schemes": [p.to_json() for p in portfolios]}
        (self.root / "parsed" / f"{sha}.json").write_text(json.dumps(body))

    def parsed(self, sha: str) -> list[SchemePortfolio]:
        """A file's portfolios; re-parsed from the kept original when the parser has changed since."""
        if sha not in self._parsed:
            p = self.root / "parsed" / f"{sha}.json"
            data = json.loads(p.read_text()) if p.exists() else None
            if isinstance(data, dict) and data.get("parser") == PARSER_VERSION:
                self._parsed[sha] = [SchemePortfolio.from_json(d) for d in data["schemes"]]
            else:
                raw = next((self.root / "files").glob(f"{sha}.*"), None)
                try:
                    got = parse_file(raw.read_bytes(), raw.name) if raw else []
                except AmcPortfolioError:
                    got = []
                if raw:
                    self._write_parsed(sha, got)
                self._parsed[sha] = got
        return self._parsed[sha]

    # -- links between AMFI scheme codes and portfolios
    def set_alias(self, scheme_code: str, key: str | None) -> None:
        with self._lock:
            idx = self.index()
            if key:
                idx["aliases"][scheme_code] = key
            else:
                idx["aliases"].pop(scheme_code, None)
            self._write_index(idx)

    def key_for(self, scheme_code: str | None, name: str | None) -> str | None:
        alias = self.index()["aliases"].get(scheme_code or "")
        return alias or (scheme_key(name) if name else None)

    def find(self, key: str) -> list[Found]:
        """Every stored portfolio of a scheme, one per month (the most recently fetched file wins), newest first."""
        idx = self.index()
        by_month: dict[str, tuple[str, str, dict[str, Any]]] = {}
        for sha, meta in sorted(idx["files"].items(), key=lambda kv: kv[1].get("fetched_at") or ""):
            for s in meta.get("schemes", []):
                if s.get("key") == key:
                    by_month[s.get("as_of") or "unknown"] = (sha, s["sheet"], meta)
        out = []
        for _, (sha, sheet, meta) in sorted(by_month.items(), reverse=True):
            p = next((x for x in self.parsed(sha) if x.sheet == sheet and x.key == key), None)
            if p is not None:
                out.append(Found(sha, p, meta))
        return out

    def newest(self, key: str | None) -> tuple[bool, date | None]:
        """(any stored file holds this scheme, its newest portfolio date) from the index alone: nothing is parsed,
        so pages can ask cheaply (a Tata workbook holds 65 schemes)."""
        dates = [s.get("as_of") for m in self.index()["files"].values() for s in m.get("schemes", [])
                 if key and s.get("key") == key]  # fmt: skip
        known = [d for d in dates if d]
        return bool(dates), (date.fromisoformat(max(known)) if known else None)

    def latest(self, key: str | None) -> Found | None:
        if not key:
            return None
        got = self.find(key)
        return got[0] if got else None

    def schemes(self) -> list[dict[str, Any]]:
        """Every scheme with at least one stored month: key, name, months, benchmark."""
        acc: dict[str, dict[str, Any]] = {}
        for meta in self.index()["files"].values():
            for s in meta.get("schemes", []):
                a = acc.setdefault(
                    s["key"], {"key": s["key"], "name": s["name"], "months": set(), "benchmark": None}
                )
                if s.get("as_of"):
                    a["months"].add(s["as_of"][:7])
                a["benchmark"] = a["benchmark"] or s.get("benchmark")
        return [
            {**v, "months": sorted(v["months"], reverse=True)}
            for v in sorted(acc.values(), key=lambda v: v["name"])
        ]

    # -- AMFI cap list
    def cap_list(self) -> dict[str, Any] | None:
        p = self.root / "amfi_cap_list.json"
        return json.loads(p.read_text()) if p.exists() else None

    def save_cap_list(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "amfi_cap_list.json").write_text(json.dumps(data))


def cap_lookup(store: PortfolioStore) -> tuple[Callable[[str], str | None], dict[str, Any] | None]:
    cl = store.cap_list()
    by = (cl or {}).get("by_isin") or {}
    # an ISIN changes when a company splits or consolidates its shares (INE237A01028 -> INE237A01036): the first nine
    # characters (country, issuer, security type 01 = equity) still name the same company's equity shares
    by_issuer = {k[:9]: v for k, v in by.items() if k[7:9] == "01"}

    def cap_of(isin: str) -> str | None:
        hit = by.get(isin) or (by_issuer.get(isin[:9]) if isin[7:9] == "01" else None)
        return hit[1] if hit else None

    return cap_of, ({k: cl.get(k) for k in ("as_of", "title", "url", "fetched_at")} if cl else None)


# ----------------------------------------------------------------------------------------------- downloads
def _client() -> Any:
    from finresearch.adapters.http import PoliteClient

    return PoliteClient(cache_dir=None, timeout=60.0, headers={"Accept": "*/*"})


async def fetch_url(store: PortfolioStore, url: str, *, amc: str | None = None, client: Any = None
                    ) -> tuple[str, list[SchemePortfolio]]:  # fmt: skip
    """Download one allow-listed file and store it."""
    url = check_url(url)
    if client is not None:  # tests pass a fake client
        got = await client.get(url)
        if not got.ok:
            raise AmcPortfolioError(f"the fund house answered HTTP {got.status} for {url}")
        content = got.content
    else:
        url, content = await _download_checked(url)
    name = url.rsplit("/", 1)[-1].split("?", 1)[0] or "download.xlsx"
    return store.add_file(content, name, url=url, amc=amc)


MAX_REDIRECTS = 5
MAX_FILE_BYTES = 60 * 1024 * 1024  # a month's workbook for a whole fund house is a few MB


async def _download_checked(url: str) -> tuple[str, bytes]:
    """GET with redirects followed by hand: every hop must pass `check_url` (https, allow-listed AMC host), so a
    redirect can't take the fetch anywhere else; the body is capped. Returns the final URL and the bytes."""
    import httpx

    from finresearch.adapters.http import BROWSER_HEADERS

    async with httpx.AsyncClient(follow_redirects=False, timeout=60.0,
                                 headers={**BROWSER_HEADERS, "Accept": "*/*"}) as c:  # fmt: skip
        for _ in range(MAX_REDIRECTS + 1):
            async with c.stream("GET", url) as r:
                if r.is_redirect:
                    nxt = r.headers.get("location", "")
                    url = check_url(str(httpx.URL(url).join(nxt)))
                    continue
                if r.status_code >= 400:
                    raise AmcPortfolioError(f"the fund house answered HTTP {r.status_code} for {url}")
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise AmcPortfolioError(
                            f"{url}: file larger than {MAX_FILE_BYTES // 1024 // 1024} MB"
                        )
                    chunks.append(chunk)
                return url, b"".join(chunks)
    raise AmcPortfolioError(f"{url}: more than {MAX_REDIRECTS} redirects")


def months_back(today: date, n: int) -> list[tuple[int, int]]:
    """(year, month) of the `n` calendar months before `today`'s month, newest first (the month in progress has no
    month-end portfolio yet)."""
    y, m, out = today.year, today.month, []
    for _ in range(n):
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        out.append((y, m))
    return out


async def _links(http: Any, source: str, key: str, months: int, today: date) -> list[Any]:
    """The scheme's monthly file links: one GET of the house's page, or (quant) one POST per month, newest first,
    until `months` months list the scheme (at most months + 1 requests)."""
    src = AMC_SOURCES[source]
    if source != "quant":
        page = await http.get(src.page)
        if not page.ok:
            raise AmcPortfolioError(f"{src.amc}'s disclosure page answered HTTP {page.status}")
        return pick_links(source, discover_links(source, page.text, src.page), key, months)
    got: list[Any] = []
    for y, m in months_back(today, months + 1):
        page = await http.post(QUANT_LIST_URL, content=quant_list_body(y, m),
                               headers={"Content-Type": "application/json; charset=utf-8", "Referer": src.page,
                                        "X-Requested-With": "XMLHttpRequest"})  # fmt: skip
        if not page.ok:
            raise AmcPortfolioError(f"{src.amc}'s monthly list answered HTTP {page.status}")
        got += pick_links(source, discover_links(source, page.text, src.page), key, months)
        if len({x.month for x in got}) >= months:
            break
    return got


async def fetch_scheme(store: PortfolioStore, key: str, amc: str | None, *, months: int = 1, client: Any = None,
                       today: date | None = None) -> dict[str, Any]:  # fmt: skip
    """Find and download a scheme's latest `months` monthly files from its fund house (PPFAS, Nippon, DSP, quant,
    Tata)."""
    source = source_for_amc(amc, key)
    if source is None or AMC_SOURCES[source].mode != "auto":
        raise AmcPortfolioError("this fund house's files cannot be found automatically: paste the file link or "
                                "upload the file")  # fmt: skip
    src = AMC_SOURCES[source]
    own = client is None
    http = client or _client()
    fetched, errors = [], []
    try:
        links = await _links(http, source, key, months, today or date.today())
        if not links:
            raise AmcPortfolioError(
                f"no monthly portfolio file for this scheme found on {src.page} (not published yet, or the page "
                "has changed)"
            )
        have = {m.get("url") for m in store.index()["files"].values()}
        for link in links:
            if link.url in have:
                continue
            try:
                # live: the hop-checked download (the page client follows redirects); tests: their fake
                await fetch_url(store, link.url, amc=src.amc, client=None if own else http)
                fetched.append({"url": link.url, "month": link.month})
            except AmcPortfolioError as e:
                errors.append({"url": link.url, "month": link.month, "error": str(e)})
    finally:
        if own:
            await http.aclose()
    found = store.find(key)
    return {"source": source, "page": src.page, "fetched": fetched, "errors": errors,
            "months": [f.month for f in found], "matched": bool(found)}  # fmt: skip


async def refresh_cap_list(store: PortfolioStore, *, client: Any = None, today: date | None = None) -> bool:
    """Download AMFI's latest average-market-cap list when ours is missing or a month old. True if it changed."""
    cur = store.cap_list()
    today = today or date.today()
    if (
        cur
        and cur.get("fetched_at")
        and (today - date.fromisoformat(cur["fetched_at"][:10])).days < CAP_LIST_MAX_AGE_DAYS
    ):
        return False
    own = client is None
    http = client or _client()
    try:
        for url in AMFI_CAP_LIST_URLS:
            try:
                got = await http.get(url)
            except Exception:
                continue
            if got.ok and got.content[:2] == b"PK":
                data = parse_amfi_cap_list(got.content)
                if cur and cur.get("as_of") and data.get("as_of") and data["as_of"] < cur["as_of"]:
                    continue
                store.save_cap_list({**data, "url": url, "fetched_at": fetched_at_now()})
                return True
    finally:
        if own:
            await http.aclose()
    if cur:  # keep the old list, but do not retry on every request
        store.save_cap_list({**cur, "fetched_at": fetched_at_now()})
    return False


# ----------------------------------------------------------------------------------------------- views
def lines_of(p: SchemePortfolio) -> list[FundLine]:
    return [FundLine(h.isin, h.name, h.weight, h.kind, h.industry) for h in p.holdings]


def equity_weights(p: SchemePortfolio) -> dict[str, Decimal]:
    return combine((h.isin, h.weight) for h in p.holdings if h.kind in EQUITY_KINDS)


def stale(p: SchemePortfolio, today: date) -> bool:
    return p.as_of is None or (today - p.as_of).days > STALE_DAYS


def file_status(found: Found | None, today: date) -> dict[str, Any]:
    if found is None:
        return {"available": False}
    p = found.portfolio
    return {"available": True, "as_of": p.as_of.isoformat() if p.as_of else None, "month": found.month,
            "stale": stale(p, today), "age_days": (today - p.as_of).days if p.as_of else None,
            "scheme_name": p.scheme_name, "sheet": p.sheet, "sha": found.sha, "url": found.meta.get("url"),
            "filename": found.meta.get("filename"), "fetched_at": found.meta.get("fetched_at"),
            "benchmark": p.benchmark, "warnings": p.warnings,
            "equity_pct": _f(p.weight_of(*EQUITY_KINDS)), "holdings": len(p.holdings)}  # fmt: skip


def overlap_json(a: Found, b: Found, top: int = 10) -> dict[str, Any]:
    names = {h.isin: h.name for h in (*a.portfolio.holdings, *b.portfolio.holdings)}
    o = overlap(equity_weights(a.portfolio), equity_weights(b.portfolio))
    return {"overlap_pct": _f(o.overlap), "common": o.common, "equity_a": _f(o.weight_a), "equity_b": _f(o.weight_b),
            "months": [a.month, b.month],
            "top": [{"isin": k, "name": names.get(k, k), "a": _f(wa), "b": _f(wb), "min": _f(m)}
                    for k, wa, wb, m in o.items[:top]]}  # fmt: skip


# benchmark proxies: an index fund/ETF tracking the named index, from the stored files (no free index weights exist:
# niftyindices publishes constituents without weights)
# the scheme key must END with the index name plus generic words only ("… Nifty 50 BeES", "… Index Fund - Nifty 50
# Plan"), so "Nifty Midcap 150 Quality 50" or "Nifty 50 Value 20" never stand in for "Nifty Midcap 150" / "Nifty 50"
_GENERIC_TAIL = r"(?:\s+(?:etf|index|fund|bees|plan|exchange|traded))*"


def bench_norm(name: str | None) -> str | None:
    if not name:
        return None
    s = name.lower()
    s = re.sub(r"\(?\btri\b\)?|total returns? index|\bindex\b|[()]", " ", s)
    s = s.replace("s&p ", "").replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b(mid|small|large) cap\b", r"\1cap", s)
    return re.sub(r"\s+", " ", s).strip() or None


def proxy_candidates(store: PortfolioStore, bench: str | None) -> list[dict[str, Any]]:
    n = bench_norm(bench)
    if not n:
        return []
    rx = re.compile(rf"(?:^|\s){re.escape(n)}{_GENERIC_TAIL}$")
    out = []
    for s in store.schemes():
        k = s["key"]
        if (
            re.search(r"\b(etf|index|bees)\b", k)
            and not re.search(r"\bfof\b|fund of fund", k)
            and rx.search(k)
        ):
            out.append(s)
    return out


def active_share_json(store: PortfolioStore, found: Found, bench_key: str | None = None) -> dict[str, Any]:
    p = found.portfolio
    method = ("½ Σ |w_fund − w_index| over the union of stocks, both re-scaled to 100 % equity (Cremers & "
              "Petajisto 2009). Index weights come from an index fund/ETF's own month-end portfolio: a proxy.")  # fmt: skip
    base = {"benchmark": p.benchmark, "benchmark_source": "the fund's own monthly file" if p.benchmark else None,
            "method": method}  # fmt: skip
    cands = proxy_candidates(store, p.benchmark)
    key = bench_key or (cands[0]["key"] if cands else None)
    if not key:
        why = ("the file does not name a benchmark" if not p.benchmark else
               f"no index fund tracking {p.benchmark} is stored. Upload or fetch the month-end portfolio of an index "
               "fund or ETF on that index to compute it.")  # fmt: skip
        return {**base, "active_share": None, "reason": why, "candidates": cands}
    proxy = store.latest(key)
    if proxy is None:
        return {
            **base,
            "active_share": None,
            "reason": "the chosen proxy has no stored portfolio",
            "candidates": cands,
        }
    a = active_share(equity_weights(p), equity_weights(proxy.portfolio))
    return {**base, "active_share": _f(a), "proxy": {"key": key, "name": proxy.portfolio.scheme_name,
                                                     "month": proxy.month, "chosen": bool(bench_key)},
            "same_month": proxy.month == found.month, "candidates": cands,
            "reason": None if a is not None else "one side has no equity holdings"}  # fmt: skip


def style_json(store: PortfolioStore, key: str, cap_of: Callable[[str], str | None]) -> list[dict[str, Any]]:
    series = style_series([(f.month, lines_of(f.portfolio)) for f in store.find(key)], cap_of)
    return [{"month": m.month, "equity_pct": _f(m.equity_pct), "drift_pct": _f(m.drift), "holdings": m.holdings,
             "caps": {k: _f(v) for k, v in m.caps.items()},
             "sectors": [{"label": k, "pct": _f(v)} for k, v in list(m.sectors.items())[:8]]} for m in series]  # fmt: skip


def fund_detail(
    store: PortfolioStore, key: str, today: date, *, bench_key: str | None = None
) -> dict[str, Any]:
    found = store.latest(key)
    cap_of, cap_meta = cap_lookup(store)
    out: dict[str, Any] = {"key": key, "file": file_status(found, today), "cap_list": cap_meta, "limits": LIMITS,
                           "disclaimer": DISCLAIMER}  # fmt: skip
    if found is None:
        return out
    p = found.portfolio
    by_kind: dict[str, Decimal] = {}
    for h in p.holdings:
        by_kind[h.kind] = by_kind.get(h.kind, ZERO) + h.weight
    top = sorted((h for h in p.holdings if h.kind in EQUITY_KINDS), key=lambda h: -h.weight)[:15]
    out.update({
        "top": [{"isin": h.isin, "name": h.name, "industry": h.industry, "weight": _f(h.weight),
                 "cap": "Foreign" if h.kind == "foreign_equity" else (cap_label(cap_of(h.isin)) or "Unclassified")} for h in top],
        "kinds": [{"kind": k, "label": BUCKET_LABEL.get(k, k), "pct": _f(v)}
                  for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])],
        "remainder_pct": _f(Decimal(100) - sum(by_kind.values(), ZERO)),
        "active_share": active_share_json(store, found, bench_key),
        "style": style_json(store, key, cap_of),
    })  # fmt: skip
    return out


# ----------------------------------------------------------------------------------------------- the whole portfolio
@dataclass
class HeldFund:
    code: str | None
    name: str
    key: str | None
    value: Decimal
    found: Found | None
    amc: str | None


def held_funds(rows: Iterable[dict[str, Any]], store: PortfolioStore, amfi: dict[str, Any] | None = None
               ) -> list[HeldFund]:  # fmt: skip
    """The user's open mutual-fund holdings (from report.snapshot rows), one per scheme, with their portfolio file."""
    amfi = amfi or {}
    acc: dict[str, HeldFund] = {}
    for r in rows:
        if r.get("asset_type") != "mf" or r.get("closed"):
            continue
        code = r.get("scheme_code")
        nav_row = amfi.get(code or "")
        name = getattr(nav_row, "name", None) or r.get("name") or code or "Fund"
        key = store.key_for(code, name)
        ident = key or code or name
        value = Decimal(str(r["value"])) if r.get("value") is not None else ZERO
        if ident in acc:
            acc[ident].value += value
        else:
            acc[ident] = HeldFund(code, name, key, value, store.latest(key), getattr(nav_row, "amc", None))
    return list(acc.values())


def lookthrough_exposure(session: Any, prices: dict[int, Any], today: date, *, store: PortfolioStore | None = None,
                         amfi: dict[str, Any] | None = None, top: int = 25) -> dict[str, Any]:  # fmt: skip
    """The user's whole portfolio looked through the funds' latest month-end portfolios. JSON-ready.

    `prices` as from portfolio.valuation.fetch_prices; `amfi` = {scheme code: SchemeNav} for scheme names/AMCs."""
    from finresearch.portfolio.report import snapshot

    store = store or PortfolioStore()
    snap = snapshot(session, prices, today)
    rows = [r for r in snap["holdings"] if not r["closed"]]
    cap_of, cap_meta = cap_lookup(store)
    funds = held_funds(rows, store, amfi)
    direct = []
    for r in rows:
        if r["asset_type"] == "mf" or r.get("value") is None:
            continue
        equity = r["asset_type"] == "stock" and r.get("tax_class") == "equity"
        direct.append(DirectInput(key=(r.get("isin") or r.get("nse_symbol") or r.get("bse_code") or r["name"]).upper(),
                                  name=r["name"], value=Decimal(str(r["value"])), sector=r.get("sector"),
                                  equity=equity, cap=r.get("cap_bucket") if equity else None))  # fmt: skip
    fin = [
        FundInput(f.name, f.value, lines_of(f.found.portfolio) if f.found else None)
        for f in funds
        if f.value > 0
    ]
    ex = lookthrough(direct, fin, cap_of)
    total = ex.total

    def pct(v: Decimal, of: Decimal) -> float | None:
        return _f(v * 100 / of) if of > 0 else None

    have = [f for f in funds if f.found is not None]
    matrix = []
    for i, a in enumerate(have):
        for b in have[i + 1 :]:
            o = overlap_json(a.found, b.found, top=5)  # type: ignore[arg-type]
            matrix.append({"a": a.code or a.name, "b": b.code or b.name, **o})
    unpriced = sum(1 for r in rows if r.get("value") is None)
    eq_w = {st.key: st.value for st in ex.stocks}
    conc = hhi(eq_w)
    top5 = sum((st.value for st in ex.stocks[:5]), ZERO)
    fund_value = sum((f.value for f in funds), ZERO)
    covered = sum((f.value for f in funds if f.found is not None), ZERO)
    top_sector = next(iter(ex.sectors.items()), None)
    return {
        "as_of": today.isoformat(), "total": _f(total), "equity": _f(ex.equity),
        "equity_pct": pct(ex.equity, total), "reconciliation": _f(ex.check(), 6),
        "stocks": [{"key": s.key, "name": s.name, "value": _f(s.value), "pct": pct(s.value, total),
                    "pct_equity": pct(s.value, ex.equity), "sector": s.sector, "cap": s.cap, "kind": s.kind,
                    "routes": [{"source": k, "value": _f(v)} for k, v in sorted(s.routes.items(), key=lambda kv: -kv[1])]}
                   for s in ex.stocks[:top]],
        "stocks_count": len(ex.stocks),
        "sectors": [{"label": k, "value": _f(v), "pct_equity": pct(v, ex.equity)} for k, v in ex.sectors.items()],
        "caps": [{"label": k, "value": _f(v), "pct_equity": pct(v, ex.equity)} for k, v in ex.caps.items()],
        "buckets": [{"kind": k, "label": BUCKET_LABEL.get(k, k), "value": _f(v), "pct": pct(v, total)}
                    for k, v in sorted(ex.buckets.items(), key=lambda kv: -kv[1])],
        "redundancy_pct": _f(ex.redundancy),
        # for the Concentration tab (WP-A): the same measures on the looked-through equity
        "concentration": {
            "hhi": _f(conc[0], 4) if conc else None, "n_effective": _f(conc[1], 1) if conc else None,
            "top5_pct_equity": pct(top5, ex.equity), "largest": ex.stocks[0].name if ex.stocks else None,
            "largest_pct": pct(ex.stocks[0].value, total) if ex.stocks else None,
            "top_sector": top_sector[0] if top_sector else None,
            "top_sector_pct": pct(top_sector[1], total) if top_sector else None,
            "fund_coverage_pct": pct(covered, fund_value),
            "how": "HHI = Σ w² over the looked-through stocks (weights re-scaled to 100 % of equity); N_eff = 1/HHI.",
        },
        "funds": [{"code": f.code, "name": f.name, "key": f.key, "value": _f(f.value), "pct": pct(f.value, total),
                   "amc": f.amc, "source": source_for_amc(f.amc, f.key or ""),
                   "file": file_status(f.found, today)} for f in funds],
        "overlap": matrix,
        "coverage": coverage_from_rows(rows, store, today, amfi),
        "unpriced": unpriced,
        "cap_list": cap_meta, "limits": LIMITS, "disclaimer": DISCLAIMER,
    }  # fmt: skip


# ----------------------------------------------------------------------------------------------- coverage (#214)
@dataclass(frozen=True)
class FundValue:
    code: str | None
    name: str
    value: Decimal | None  # None: no price today
    amc: str | None = None


def fund_status(store: PortfolioStore, code: str | None, name: str, amc: str | None, today: date
                ) -> tuple[str, str, date | None]:  # fmt: skip
    """(status, reason, portfolio date) of one held fund: looked_through | not_fetched | unsupported."""
    have, as_of = store.newest(store.key_for(code, name))
    if have:
        old = as_of is None or (today - as_of).days > STALE_DAYS
        return "looked_through", (as_of.strftime("%b %Y") if as_of else "undated") + " portfolio" + (
            " (stale)" if old else ""), as_of  # fmt: skip
    house = house_for(amc, name)
    if house in AMC_SOURCES:
        src = AMC_SOURCES[house]
        if src.mode == "auto":
            return (
                "not_fetched",
                f"{src.amc}: supported, no file fetched yet (the monthly job or Fetch)",
                None,
            )
        return "not_fetched", f"{src.amc}: paste the file link or upload the file", None
    if house in AMC_UNSUPPORTED:
        u = AMC_UNSUPPORTED[house]
        return "unsupported", f"{u.amc}: {u.reason}", None
    return "unsupported", UNKNOWN_HOUSE, None


def coverage(total: Decimal | None, funds: Iterable[FundValue], store: PortfolioStore, today: date
             ) -> dict[str, Any]:  # fmt: skip
    """How much of the portfolio the look-through can see: the share of the portfolio's value in funds whose holdings
    are NOT looked through, and why per fund. Unknown stays unknown: a fund without a price (or no portfolio value)
    makes the share None, never 0."""
    by_key: dict[str, dict[str, Any]] = {}
    for f in funds:
        status, reason, as_of = fund_status(store, f.code, f.name, f.amc, today)
        k = store.key_for(f.code, f.name) or f.code or f.name
        cur = by_key.get(k)
        if cur is None:
            by_key[k] = {"code": f.code, "name": f.name, "value": f.value, "status": status, "reason": reason,
                         "month": as_of.strftime("%Y-%m") if as_of else None,
                         "stale": status == "looked_through" and (as_of is None or (today - as_of).days > STALE_DAYS)}  # fmt: skip
        else:  # the same scheme in two folios/plans
            cur["value"] = None if cur["value"] is None or f.value is None else cur["value"] + f.value
    rows = list(by_key.values())
    unpriced = sum(1 for r in rows if r["value"] is None)
    fund_value = sum((r["value"] for r in rows if r["value"] is not None), ZERO)
    seen = sum((r["value"] for r in rows if r["value"] is not None and r["status"] == "looked_through"), ZERO)
    blind = fund_value - seen
    known = bool(total) and total > 0 and not unpriced  # type: ignore[operator]
    n = {
        s: sum(1 for r in rows if r["status"] == s) for s in ("looked_through", "not_fetched", "unsupported")
    }
    blind_pct = _f(blind * 100 / total) if known else None  # type: ignore[operator]
    fund_pct = _f(fund_value * 100 / total) if known else None  # type: ignore[operator]
    missing = n["not_fetched"] + n["unsupported"]
    why = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in (("unsupported", n["unsupported"]),
                                                               ("not_fetched", n["not_fetched"])) if v)  # fmt: skip
    if not rows:
        text = None
    elif blind_pct is None:
        text = (
            f"Direct stocks only — the share of the portfolio in funds not looked through is unknown "
            f"({unpriced} fund(s) without a price today; {missing} of {len(rows)} funds not looked through)"
        )
    elif missing:
        text = (
            f"Direct stocks only — {blind_pct:.0f} % of the portfolio is in funds not looked through "
            f"({missing} of {len(rows)} funds: {why})"
        )
    else:
        text = (
            f"Direct stocks only — funds are {fund_pct:.0f} % of the portfolio; all {len(rows)} are looked "
            "through on the Look-through page"
        )
    return {"funds_total": len(rows), "looked_through": n["looked_through"], "not_fetched": n["not_fetched"],
            "unsupported": n["unsupported"], "stale": sum(1 for r in rows if r["stale"]), "unpriced": unpriced,
            "fund_pct": fund_pct, "not_looked_through_pct": blind_pct, "text": text,
            "funds": [{**r, "value": _f(r["value"])} for r in rows]}  # fmt: skip


def coverage_from_rows(rows: Iterable[dict[str, Any]], store: PortfolioStore, today: date,
                       amfi: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """`coverage` over report.snapshot rows (open holdings; the total is the priced value, as on the cards)."""
    amfi = amfi or {}
    rows = [r for r in rows if not r.get("closed")]
    total = sum((Decimal(str(r["value"])) for r in rows if r.get("value") is not None), ZERO)
    funds = []
    for r in rows:
        if r.get("asset_type") != "mf":
            continue
        nav = amfi.get(r.get("scheme_code") or "")
        funds.append(FundValue(r.get("scheme_code"), getattr(nav, "name", None) or r.get("name") or "Fund",
                               Decimal(str(r["value"])) if r.get("value") is not None else None,
                               getattr(nav, "amc", None)))  # fmt: skip
    return coverage(total, funds, store, today)
