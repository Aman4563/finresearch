"""#366: the rate and TRI commands pass their options through (the store is replaced; no network, no database)."""

from __future__ import annotations

from datetime import date

from typer.testing import CliRunner

from finresearch.cli import app


def test_rates_backfill_passes_series_window_and_pause(monkeypatch):
    from finresearch.refseries import rates

    seen = {}

    async def fake(series, start, end=None, **kw):
        seen.update(series=list(series), start=start, end=end, pause=kw.get("pause"))
        return {"planned": 0, "loaded": 0, "no_file": 0, "failed": {}, "rows": 0}

    monkeypatch.setattr(rates, "backfill", fake)
    res = CliRunner().invoke(app, ["rates", "backfill", "--series", "tbill", "--series", "gsec_par",
                                   "--from", "2022-01-01", "--to", "2022-03-31", "--delay", "3"])  # fmt: skip
    assert res.exit_code == 0, res.output
    assert seen == {
        "series": ["tbill", "gsec_par"],
        "start": date(2022, 1, 1),
        "end": date(2022, 3, 31),
        "pause": 3.0,
    }


def test_rates_backfill_defaults_to_every_series(monkeypatch):
    from finresearch.refseries import rates

    seen = {}

    async def fake(series, start, end=None, **kw):
        seen["series"] = list(series)
        return {"planned": 0, "loaded": 0, "no_file": 0, "failed": {}, "rows": 0}

    monkeypatch.setattr(rates, "backfill", fake)
    assert CliRunner().invoke(app, ["rates", "backfill", "--from", "2022-01-01"]).exit_code == 0
    assert seen["series"] == list(rates.SERIES)


def test_rates_backfill_exits_nonzero_when_stopped(monkeypatch):
    from finresearch.refseries import rates

    async def fake(series, start, end=None, **kw):
        return {"planned": 1, "loaded": 0, "no_file": 0, "failed": {"tbill 2022-01": "x"}, "rows": 0,
                "stopped": "FBIL refused 2 requests in a row"}  # fmt: skip

    monkeypatch.setattr(rates, "backfill", fake)
    assert CliRunner().invoke(app, ["rates", "backfill", "--from", "2022-01-01"]).exit_code == 1
