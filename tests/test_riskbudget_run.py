"""P4.2 Stage 2 (#395): the seen-period study's run: refused before any read unless the registration holds, a history
database without SPAN ranges refused up front, the report (aggregates only, labelled seen), and the CLI. Synthetic
history only; no database row is written."""

from __future__ import annotations

import json
import math
import random
from dataclasses import replace
from datetime import date

import pytest
import riskbudget_synth as rs
from typer.testing import CliRunner

from finresearch.backtest import specs as bspecs
from finresearch.riskbudget import prereg, replay, run, specs, stats


class NoReads:
    def __getattr__(self, name: str):
        raise AssertionError(f"the history was read ({name}) before the registration was checked")


def test_the_study_is_refused_before_any_read_while_the_registration_differs(monkeypatch) -> None:
    from finresearch.fincalc import tradeplan

    monkeypatch.setattr(tradeplan, "BOOK_BUDGET_PCT", 10.0)
    with pytest.raises(prereg.PreregistrationError):
        run.study(NoReads(), source="x")


def test_a_history_database_without_span_ranges_is_refused(tmp_path) -> None:
    with pytest.raises(run.Refused, match="span_param"):
        run.open_history(f"sqlite:///{tmp_path / 'empty.db'}")


def test_the_history_opens_read_only_with_the_stored_span_ranges(env) -> None:
    h = run.open_history(env.database_url)
    assert h.span_ranges is True and h.url == env.database_url


@pytest.fixture(scope="module")
def short():
    h = rs.history(date(2022, 3, 1), date(2022, 8, 31))
    sp = [replace(bspecs.by_key(k), iv_pct_min=None) for k in specs.SPEC_KEYS]
    return replay.replay(h, sp, date(2022, 3, 1), date(2022, 8, 31))


def test_the_report_is_aggregates_labelled_seen(short) -> None:
    rep = run.report(short)
    u, b = short.arms["uncapped"], short.arms["budgeted"]
    assert rep["label"] == specs.LABEL and rep["partial"] is True  # it ends in Aug-2022, not Oct-2026
    assert (rep["first_session"], rep["last_session"]) == ("2022-03-01", "2022-08-31")
    mean = lambda arm: sum(r for _, r in arm.risk) / len(arm.risk)  # noqa: E731
    assert rep["control"]["k"] == pytest.approx(mean(b) / mean(u))
    assert rep["secondary"]["entries_budgeted"] == len(b.entries) == rep["arms"]["budgeted"]["entries"]
    assert rep["primary"]["passes"] is False and "6 calendar months" in rep["primary"]["reason"]
    assert rep["arms"]["uncapped"]["closed_trades"] == len(u.trades())
    assert rep["arms"]["budgeted"]["refused"] == dict(b.refused)
    rows = [
        v
        for a in rep["arms"].values()
        for v in a.values()
        if isinstance(v, list) and v and isinstance(v[0], dict)
    ]
    assert rows == []  # aggregates only: no trade rows
    worst = min(sum(p) for _, p in stats.months(u.equity, bspecs.BOOK.capital))
    assert rep["raw_cut"]["worst_month"]["uncapped"] == pytest.approx(worst)


def ivs(days: list[date], seed: int = 3) -> dict[date, float]:
    rnd, v, out = random.Random(seed), 15.0, {}
    for d in days:
        v = min(30.0, max(9.0, v + rnd.gauss(0, 0.6)))
        out[d] = v
    return out


def test_the_registered_study_end_to_end_on_thirteen_synthetic_months() -> None:
    start, end = date(2022, 1, 3), date(2023, 1, 31)
    days = [d for d in rs.fs.path(start, end) if d.weekday() < 5]
    h = rs.history(start, end, iv30=ivs(days))
    rep = run.study(h, source="synthetic")
    assert rep["registration"] == prereg.HASH and rep["spec_version"] == specs.SPEC_VERSION
    assert rep["primary"]["months"] == 13 and rep["primary"]["worst_month"]["p"] is not None
    assert rep["arms"]["uncapped"]["entries"] > rep["arms"]["budgeted"]["entries"] > 0
    assert rep["arms"]["budgeted"]["refused"].get("common shock (B_shock): unknown (beta)", 0) > 0
    assert math.isfinite(rep["control"]["k"]) and rep["source"] == "synthetic" and rep["partial"] is True


def test_the_cli_refuses_without_span_ranges_and_writes_the_report(tmp_path, monkeypatch) -> None:
    from finresearch.cli import app

    r = CliRunner().invoke(app, ["fno", "risk-budget", "--history-url", f"sqlite:///{tmp_path / 'e.db'}"])
    assert r.exit_code == 2 and "span_param" in r.output
    monkeypatch.setattr(run, "open_history", lambda url: rs.history(date(2022, 3, 1), date(2022, 4, 29)))
    out = tmp_path / "rep.json"
    r = CliRunner().invoke(app, ["fno", "risk-budget", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert json.loads(out.read_text())["registration"] == prereg.HASH and "seen" in r.output
