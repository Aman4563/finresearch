"""`finresearch fno vrp` (P3.6, #389): the recent window runs the registered study on both indices read-only, and the
vault read is refused BEFORE the guard is asked when the common range, the output path or a sourced charges table is
missing (the read could not be repeated)."""

import json
import re
from datetime import date, timedelta

from typer.testing import CliRunner

from finresearch.fincalc import realvol as rv
from finresearch.vrp import collect as co
from finresearch.vrp import run
from finresearch.vrp import study as st


def inputs() -> run.Inputs:
    d0 = date(2022, 1, 27)
    days, px = [], 17000.0
    for i in range(70):
        d = d0 + timedelta(i)
        o, c = px * 1.001, px * (1.004 if i % 2 else 0.997)
        days.append(
            st.Day(d, rv.Bar(d, o, max(o, c) * 1.002, min(o, c) * 0.998, c, px), 0.03, None, 0.031, 17.0)
        )
        px = c
    tr = co.Trade(d0, date(2022, 2, 24), 17000.0, 50, (co.Mark(d0, 17000.0, 300.0, 290.0, 5000, 5000),), 17100.0,
                  ("call", "put"))  # fmt: skip
    return run.Inputs(days, {d0}, days[-1].day, [tr], [], {d0: 0.5}, {d0: 0.4})


def test_recent_window_reports_both_indices(env, monkeypatch, tmp_path) -> None:
    from finresearch.cli import app
    from finresearch.vrp import data, prereg

    seen = []

    def fake_load(symbol, start, end, *, lots, band, url=None, grant=None):
        seen.append((symbol, start, end, band, grant))
        return inputs(), {"mf30_unknown": 0}

    monkeypatch.setattr(data, "load", fake_load)
    out = tmp_path / "vrp.json"
    res = CliRunner().invoke(app, ["fno", "vrp", "--json", "--out", str(out)])
    assert res.exit_code == 0, res.output
    body = json.loads(out.read_text())
    assert [s[0] for s in seen] == ["NIFTY", "BANKNIFTY"] and seen[0][1:] == (date(2022, 1, 1), date(2026, 12, 31),
                                                                             None, None)  # fmt: skip
    assert body["registration"] == prereg.HASH and "not era-comparable" in body["label"]
    assert body["symbols"]["NIFTY"]["periods"]["seen"]["primary"]["n"] == 1
    text = CliRunner().invoke(app, ["fno", "vrp"])
    assert text.exit_code == 0 and "VRP payoff mean" in text.output and "not adopted" in text.output


def _plain(text: str) -> str:
    # CI's terminal gets rich's coloured usage panel; the escape codes split option names like --common-range
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_vault_read_is_refused_before_the_guard_without_its_inputs(env, monkeypatch, tmp_path) -> None:
    from finresearch.cli import app
    from finresearch.fincalc import charges
    from finresearch.vault import guard

    def never(*a, **k):
        raise AssertionError("the guard must not be asked")

    monkeypatch.setattr(guard, "vault_read", never)
    r = CliRunner().invoke(app, ["fno", "vrp", "--window", "vault", "--out", str(tmp_path / "v.json")])
    assert r.exit_code != 0 and "common-range" in _plain(r.output)
    r = CliRunner().invoke(app, ["fno", "vrp", "--window", "vault", "--common-range", "0.1"])
    assert r.exit_code != 0
    if any(
        charges.unknown_keys(date(y, 6, 28)) for y in range(2008, 2022)
    ):  # today's table: gaps before 2021
        r = CliRunner().invoke(app, ["fno", "vrp", "--window", "vault", "--common-range", "0.1", "--out",
                                     str(tmp_path / "v.json")])  # fmt: skip
        assert r.exit_code != 0 and "charges" in _plain(r.output)
