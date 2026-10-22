"""Build and read the cross-sectional options panel (P1.12, #374; evals/experiments/stock_option_vrp/PREREG.md):
`finresearch fno panel build`. Local work only: each session is re-parsed from the raw F&O bhavcopy zip the nightly
job already kept (fnohist.store.read_raw), so no file is downloaded again.

Behind the registered study: every build first asks prereg.require_registered() (specs hash and PREREG.md sha256)
and refuses any day in the sealed 2008-2021 vault. Portfolio-blind: the universe is every stock-option underlying in
the file (optpanel.panel.keep); the held or watched list is never read, the manifest records counts only, and the
RV20 futures come from fno_bhav, which stores every stock future. A session is written in one transaction (delete,
then insert), so a re-build is idempotent, and gets one lineage row (dq.lineage, dataset "optpanel") with the zip's
sha256. Sessions are the bhavcopy manifest's loaded days; a built session under the current registration hash is
skipped on the next run (resumable) unless `rebuild`.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from finresearch.adapters import nse_fno_bhav as fb
from finresearch.db.models import FnoBhav, FnoBhavDay, OptPanelDay, OptPanelEod, OptPanelRow
from finresearch.fincalc import fno_history as fh
from finresearch.optpanel import panel, prereg

log = logging.getLogger(__name__)
DATASET = "optpanel"
RV_LOOKBACK_DAYS = 45  # calendar days of futures read for RV20 (as fnohist.derive)
CHUNK = 2000
ROW_COLUMNS = ("day", "symbol", "instrument", "expiry", "strike", "open", "high", "low", "close", "settle", "oi",
               "chg_oi", "contracts", "turnover", "underlying", "lot_size")  # fmt: skip


def parser_version() -> str:
    """The bhavcopy parser's version and the registration it was built under (dataset_load keeps 20 characters)."""
    return f"{fb.PARSER_VERSION}+{prereg.HASH[:12]}"


def _q(x: float | None, places: int = 3) -> Decimal | None:
    return None if x is None or not math.isfinite(x) else Decimal(str(round(x, places)))


def _rv20(s: Session, day: date, symbols: list[str]) -> dict[str, float | None]:
    hist: dict[str, list[tuple[date, date, float]]] = defaultdict(list)
    lots: dict[str, dict[tuple[date, date], int]] = defaultdict(dict)
    for sym, d, exp, settle, lot in s.execute(
        select(FnoBhav.symbol, FnoBhav.day, FnoBhav.expiry, FnoBhav.settle, FnoBhav.lot_size).where(
            FnoBhav.symbol.in_(symbols), FnoBhav.instrument.in_(("IDF", "STF")),
            FnoBhav.day.between(day - timedelta(days=RV_LOOKBACK_DAYS), day))
    ):  # fmt: skip
        if settle is not None:
            hist[sym].append((d, exp, float(settle)))
        if lot:
            lots[sym][(d, exp)] = lot
    out = {}
    for sym in symbols:
        rets = [r for d, r in fh.front_future_returns(hist.get(sym, []), lots=lots.get(sym)) if d <= day]
        out[sym] = fh.realised_vol_pct(rets)
    return out


def _manifest(s: Session, day: date, **values: Any) -> None:
    stmt = pg_insert(OptPanelDay).values(day=day, attempts=1, **values)
    s.execute(stmt.on_conflict_do_update(index_elements=["day"], set_={
        **values, "attempts": OptPanelDay.attempts + 1, "built_at": stmt.excluded.built_at}))  # fmt: skip


def build_day(s: Session, day: date, fmt: fb.Format, url: str, content: bytes) -> dict[str, int]:
    """Replace `day`'s panel rows from its raw zip (in the caller's transaction) and record the file's lineage."""
    from finresearch.dq.lineage import record_load

    rows, _ = fb.parse(content, fmt, panel.keep)
    if any(r.day != day for r in rows):
        raise fb.NseError(f"the F&O bhavcopy for {day} carries another trade date")
    dp = panel.compute_day(day, rows)
    rv = _rv20(s, day, sorted(dp.rows))
    s.execute(delete(OptPanelEod).where(OptPanelEod.day == day))
    s.execute(delete(OptPanelRow).where(OptPanelRow.day == day))
    eod = []
    for sym, v in dp.rows.items():
        eod.append({"day": day, "symbol": sym, **v, "near_iv": _q(v["near_iv"]), "next_iv": _q(v["next_iv"]),
                    "iv_30d": _q(v["iv_30d"]), "skew_25d": _q(v["skew_25d"]), "rv_20": _q(rv.get(sym)),
                    "forward": _q(v["forward"], 2), "spot": _q(v["spot"], 2)})  # fmt: skip
    if eod:
        s.execute(insert(OptPanelEod), eod)
    for i in range(0, len(dp.kept), CHUNK):
        s.execute(insert(OptPanelRow), [{**{c: getattr(r, c) for c in ROW_COLUMNS}, "option_type": r.right}
                                        for r in dp.kept[i : i + CHUNK]])  # fmt: skip
    with_iv = sum(1 for v in dp.rows.values() if v["near_iv"] is not None)
    _manifest(s, day, status="built", spec_hash=prereg.HASH, underlyings=len(eod), with_iv=with_iv,
              rows_kept=len(dp.kept), error=None, **dp.totals)  # fmt: skip
    record_load(s, DATASET, day, content, url=url, parser_version=parser_version(), rows=len(eod))
    return {"underlyings": len(eod), "with_iv": with_iv, "rows_kept": len(dp.kept)}


def plan_days(s: Session, start: date, end: date, rebuild: bool = False) -> list[date]:
    """Sessions (the bhavcopy manifest's loaded days) in [start, end] still to build under the current hash."""
    days = s.scalars(select(FnoBhavDay.day).where(FnoBhavDay.day.between(start, end), FnoBhavDay.status == "loaded")
                     .order_by(FnoBhavDay.day)).all()  # fmt: skip
    if rebuild:
        return list(days)
    done = set(s.scalars(select(OptPanelDay.day).where(OptPanelDay.day.between(start, end),
                                                        OptPanelDay.status == "built",
                                                        OptPanelDay.spec_hash == prereg.HASH)))  # fmt: skip
    return [d for d in days if d not in done]


def build(start: date, end: date | None = None, *, today: date | None = None, rebuild: bool = False,
          progress: Callable[[int, int, date, str], None] | None = None) -> dict[str, Any]:  # fmt: skip
    """Build every planned session in [start, end] from the kept raw zips (module docstring). Raises
    PreregistrationError or VaultSealed before touching anything."""
    from finresearch import data_rights
    from finresearch.db import session_scope
    from finresearch.fincalc.dates import today_ist
    from finresearch.fnohist.store import read_raw
    from finresearch.vault.registry import VAULT_END, VaultSealed

    prereg.require_registered()
    if start <= VAULT_END:
        raise VaultSealed(
            f"{start:%d-%b-%Y} is inside the sealed 2008-2021 vault: the panel never reads it (#364)"
        )
    data_rights.require("fno_bhav")  # the kept zips are that dataset's files (#362)
    today = today or today_ist()
    end = min(end or today, today)
    with session_scope() as s:
        days = plan_days(s, start, end, rebuild)
    out: dict[str, Any] = {"planned": len(days), "built": 0, "no_raw": 0, "failed": {}, "underlyings": 0}
    for i, day in enumerate(days, 1):
        status = "built"
        try:
            got = read_raw(day)
            with session_scope() as s:
                if got is None:
                    status = "no_raw"
                    _manifest(s, day, status="no_raw", spec_hash=prereg.HASH, error=None)
                else:
                    out["underlyings"] += build_day(s, day, *got)["underlyings"]
            out[status] += 1
        except Exception as e:  # a bad zip or a parser error: recorded, retried on the next run
            status = "failed"
            out["failed"][day.isoformat()] = f"{type(e).__name__}: {e}"[:300]
            log.warning("options panel build failed for %s", day, exc_info=True)
            with session_scope() as s:
                _manifest(
                    s, day, status="failed", spec_hash=prereg.HASH, error=out["failed"][day.isoformat()]
                )
        if progress:
            progress(i, len(days), day, status)
    return out
