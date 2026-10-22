"""The skill monitor's cells (#378, skillmon/specs.py CELL, BENCHMARKS, PAIRING): the forecast ledger's rows grouped
per (method group, event kind, underlying), each model row paired with its benchmark's row on the same event.
Read only: `load` selects, never writes (the brief and GETs call it)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import Forecast
from finresearch.signals import ledger


@dataclass(frozen=True)
class Cell:
    method: str
    event_kind: str
    underlying: str  # the instrument for F&O, "*" otherwise


@dataclass
class CellData:
    cell: Cell
    asset: str
    benchmark: str | None
    logged: int = 0  # rows with a probability
    open: int = 0
    void: int = 0
    independent: list[Any] = field(
        default_factory=list
    )  # scored independent events, in the order outcomes were known
    pairs: list[tuple[float, float, int]] = field(
        default_factory=list
    )  # (p_model, p_benchmark, o), origin order
    unpaired: int = 0  # rows that should have been paired but had no resolved benchmark row
    mismatched: int = 0  # paired rows whose two outcomes differ: a ledger inconsistency, never scored
    h: int = 1  # the overlap of the rows the skill score uses (specs.SKILL.rows)


def benchmarks() -> dict[tuple[str | None, str], str]:
    """(asset or None for any, model method) -> benchmark method, the registered map with the live constants."""
    from finresearch.direction.live import METHOD_IMPLIED, METHOD_LOGIT
    from finresearch.fnobook.card_forecasts import RN_METHOD
    from finresearch.signals.ipo import BASE_RATE_METHOD, BLEND_METHOD
    from finresearch.signals.tradeplan import PLAN_METHOD

    return {(None, METHOD_LOGIT): METHOD_IMPLIED, (None, PLAN_METHOD): RN_METHOD,
            (None, BLEND_METHOD): BASE_RATE_METHOD, ("ipo", ledger.RUN_METHOD): BASE_RATE_METHOD}  # fmt: skip


def benchmark_of(asset: str, method: str) -> str | None:
    b = benchmarks()
    return b.get((asset, method)) or b.get((None, method))


def cell_of(f: Any) -> Cell:
    return Cell(ledger._method_group(f.method), f.event_kind, f.instrument if f.asset == "fno" else "*")


def origin(f: Any) -> date:
    start = (f.inputs or {}).get("start_date")
    return date.fromisoformat(start) if start else ledger.ist_day(f.created_at)


def pair_key(f: Any) -> tuple:
    inp = f.inputs or {}
    if "card_log_id" in inp or "card_index" in inp:
        return f.asset, f.instrument, f.event_kind, "card", f.source, inp.get("card_index", 0)
    return f.asset, f.instrument, f.event_kind, "origin", origin(f).isoformat()


def _scored(f: Any) -> bool:
    return f.status == "resolved" and f.probability is not None and f.outcome is not None


def collect(rows: Sequence[Any], close_dates: dict[str, date] | None = None) -> list[CellData]:
    """Every cell with at least one logged probability, sorted by cell."""
    by_cell: dict[tuple[str, Cell], list[Any]] = defaultdict(list)
    bench_rows: dict[tuple[str, tuple], Any] = {}
    for f in rows:
        if f.probability is None:
            continue
        by_cell[(f.asset, cell_of(f))].append(f)
        if _scored(f):  # the latest resolved row per method and event (the ledger keeps one per day anyway)
            bench_rows[(ledger._method_group(f.method), pair_key(f))] = f
    out = []
    for (asset, cell), fs in sorted(by_cell.items(), key=lambda kv: (kv[0][0], kv[0][1].method, kv[0][1].event_kind,
                                                                      kv[0][1].underlying)):  # fmt: skip
        d = CellData(cell, asset, benchmark_of(asset, cell.method), logged=len(fs),
                     open=sum(f.status == "open" for f in fs), void=sum(f.status == "void" for f in fs))  # fmt: skip
        scored = [f for f in fs if _scored(f)]
        d.independent = sorted(ledger.independent_events(scored, close_dates),
                               key=lambda f: (f.resolved_at, f.id))  # fmt: skip
        if d.benchmark is not None:
            hs = {(f.inputs or {}).get("h") for f in scored}
            window = bool(scored) and None not in hs
            d.h = max(int(h) for h in hs) if window else 1
            use = scored if window else d.independent
            for f in sorted(use, key=lambda f: (origin(f), f.id)):
                b = bench_rows.get((d.benchmark, pair_key(f)))
                if b is None:
                    d.unpaired += 1
                elif int(b.outcome) != int(f.outcome):
                    d.mismatched += 1
                else:
                    d.pairs.append((float(f.probability), float(b.probability), int(f.outcome)))
        out.append(d)
    return out


def load(s: Session) -> list[CellData]:
    rows = list(s.scalars(select(Forecast).order_by(Forecast.id)))
    closes = ledger.ipo_close_dates(s, {f.instrument for f in rows if f.asset == "ipo"})
    return collect(rows, closes)
