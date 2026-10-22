"""The skill monitor's cells (#378): ledger rows grouped per method x event kind x underlying, each model row paired
with its benchmark's row on the same event; unpaired rows are counted, never scored against a made-up benchmark."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from finresearch.direction.live import METHOD_IMPLIED, METHOD_LOGIT
from finresearch.fnobook.card_forecasts import RN_METHOD
from finresearch.signals.ipo import BASE_RATE_METHOD, BLEND_METHOD
from finresearch.signals.tradeplan import PLAN_METHOD
from finresearch.skillmon import cells

T0 = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)
_ids = iter(range(1, 10_000))


def row(method, *, asset="fno", instrument="NIFTY", kind="up_h5", p=0.6, outcome=1, status="resolved", day=0,
        inputs=None, source="direction"):  # fmt: skip
    created = T0 + timedelta(days=day)
    return SimpleNamespace(id=next(_ids), asset=asset, instrument=instrument, event_kind=kind, method=method,
                           probability=p, status=status, outcome=outcome if status == "resolved" else None,
                           inputs=inputs or {}, created_at=created, resolved_at=created + timedelta(days=8),
                           resolve_on=(created + timedelta(days=7)).date(), source=source,
                           validation_status="shadow")  # fmt: skip


def direction(method, day, p, o=1):
    start = (T0 + timedelta(days=day)).date().isoformat()
    return row(
        method,
        day=day,
        p=p,
        outcome=o,
        inputs={"symbol": "NIFTY", "start_date": start, "h": 5, "event": "up"},
    )


def by_method(out):
    return {d.cell.method: d for d in out}


def test_direction_logistic_pairs_with_implied_on_every_overlapping_row() -> None:
    rows = [direction(m, d, p) for d in range(3) for m, p in ((METHOD_LOGIT, 0.7), (METHOD_IMPLIED, 0.5))]
    out = by_method(cells.collect(rows))
    lg, im = out[METHOD_LOGIT], out[METHOD_IMPLIED]
    assert lg.cell == cells.Cell(METHOD_LOGIT, "up_h5", "NIFTY") and lg.benchmark == METHOD_IMPLIED
    assert lg.pairs == [(0.7, 0.5, 1)] * 3 and lg.h == 5 and lg.unpaired == 0
    assert len(lg.independent) == 1  # three 7-day windows a day apart: one non-overlapping event
    assert im.benchmark is None and im.pairs == []  # the benchmark has no skill score against itself


def test_a_model_row_without_its_benchmark_is_unpaired_not_scored() -> None:
    rows = [direction(METHOD_LOGIT, 0, 0.7), direction(METHOD_LOGIT, 1, 0.7), direction(METHOD_IMPLIED, 0, 0.5),
            direction(METHOD_IMPLIED, 1, 0.5, o=0)]  # fmt: skip
    lg = by_method(cells.collect(rows))[METHOD_LOGIT]
    assert lg.pairs == [(0.7, 0.5, 1)] and lg.mismatched == 1
    lg = by_method(cells.collect(rows[:2] + rows[2:3]))[METHOD_LOGIT]
    assert lg.unpaired == 1


def test_cards_pair_by_source_and_card_index() -> None:
    inp = {"card_log_id": 4, "card_index": 0, "symbol": "NIFTY", "expiry": "2026-06-25", "structure": "ic"}
    rows = [row(PLAN_METHOD, kind="card_pnl", p=0.65, inputs=inp, source="card_log:4"),
            row(RN_METHOD, kind="card_pnl", p=0.55, inputs=inp, source="card_log:4"),
            row(PLAN_METHOD, kind="card_pnl", p=0.6, inputs={**inp, "card_log_id": 5}, source="card_log:5")]  # fmt: skip
    pl = by_method(cells.collect(rows))[PLAN_METHOD]
    assert pl.benchmark == RN_METHOD and pl.h == 1
    assert len(pl.independent) == 1  # both cards: same underlying, expiry and structure
    assert pl.pairs == [(0.65, 0.55, 1)] and pl.unpaired == 0  # the independent (first) card only


def test_ipo_cells_pool_instruments_and_pair_on_the_logging_day() -> None:
    ipo = dict(asset="ipo", kind="listing_gain", source="signal:ipo")
    rows = [row(BLEND_METHOD, instrument="AAA", p=0.8, **ipo), row(BASE_RATE_METHOD, instrument="AAA", p=0.6, **ipo),
            row(BLEND_METHOD, instrument="BBB", p=0.3, outcome=0, **ipo),
            row(BASE_RATE_METHOD, instrument="BBB", p=0.6, outcome=0, day=1, **ipo)]  # fmt: skip
    closes = {"AAA": date(2026, 6, 3), "BBB": date(2026, 6, 3)}
    bl = by_method(cells.collect(rows, closes))[BLEND_METHOD]
    assert bl.cell.underlying == "*" and bl.benchmark == BASE_RATE_METHOD
    assert bl.pairs == [(0.8, 0.6, 1)] and bl.unpaired == 1  # BBB's base rate was logged on another day


def test_counts_and_rows_without_a_probability() -> None:
    rows = [direction(METHOD_LOGIT, 0, 0.7), row(METHOD_LOGIT, status="open"), row(METHOD_LOGIT, status="void"),
            row(METHOD_LOGIT, p=None)]  # fmt: skip
    lg = by_method(cells.collect(rows))[METHOD_LOGIT]
    assert (lg.logged, lg.open, lg.void, len(lg.independent)) == (3, 1, 1, 1)
    assert cells.collect([row(METHOD_LOGIT, p=None)]) == []
