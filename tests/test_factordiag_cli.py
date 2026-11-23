"""The owner's one-shot command of the stock factor diagnostics (#506), end to end on a GENERATED price cache (made-up
symbols, random walks; never market data): it checks the registration before any read, writes results.json and
RESULTS.md, refuses to replace them, is deterministic, opens no database, and reports what it cannot know as unknown."""

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import factordiag_synth as fs
import numpy as np
import pytest
from typer.testing import CliRunner

from finresearch.cli import app
from finresearch.evals import stock_backtest as bt
from finresearch.factordiag import prereg, specs, study

runner = CliRunner()
SYMBOLS = fs.names(30)
CHANGE = date(2018, 7, 1)  # SYN00..SYN02 leave the made-up index and SYN27..SYN29 join it
DAYS = fs.weekdays(date(2012, 1, 2), 2870)  # eleven years of generated sessions
ENDS = bt.month_ends(DAYS)
WARM = sum(1 for t in ENDS if DAYS.index(t) + 1 < bt.WARMUP)  # month-ends before any name has 253 bars


def _members(t: date) -> list[str]:
    return SYMBOLS[:27] if t < CHANGE else SYMBOLS[3:]


@pytest.fixture(scope="module")
def cache(tmp_path_factory) -> Path:
    """Eleven years of sessions for 30 made-up names. Each name keeps its own drift, so past winners keep winning: a
    persistent momentum effect the diagnostics should see."""
    rng = np.random.default_rng(5)
    uni = fs.universe(rng, DAYS, SYMBOLS, drift=lambda i: (i - 14.5) * 0.00006)
    market = fs.series(fs.MARKET, DAYS, fs.walk(rng, len(DAYS), vol=0.008))
    d = tmp_path_factory.mktemp("stock_backtest")
    fs.cache(d, uni, market)
    return d


@pytest.fixture()
def made_up_index(monkeypatch) -> None:
    monkeypatch.setattr(study, "load_membership", lambda root: _members)


def _run(*args: str):
    return runner.invoke(app, ["eval", "factor-diagnostics", *args])


def test_the_command_writes_a_json_and_a_report_and_never_replaces_them_unasked(
    cache, tmp_path, made_up_index
) -> None:
    out = tmp_path / "out"
    r = _run("--data-dir", str(cache), "--out-dir", str(out))
    assert r.exit_code == 0, r.output
    res = json.loads((out / "results.json").read_text())
    text = (out / "RESULTS.md").read_text()
    assert res["spec_version"] == specs.SPEC_VERSION and res["registration"] == prereg.HASH
    assert res["prereg_sha256"] == prereg.PREREG_SHA256 and res["label"] == specs.LABEL
    assert (
        res["audit"]["passed"] is True
        and res["audit"]["score_checks"] > 0
        and res["audit"]["return_checks"] > 0
    )
    p = res["panel"]
    # Jan-2012 to Nov-2022; 2012 has 261 weekdays, so its December month-end is the first with 253 bars
    assert (len(ENDS), WARM) == (131, 11)
    assert p["decision_dates"] == 131 and p["members"] == {"min": 27, "median": 27, "max": 27}
    assert p["eligible"]["max"] == 27 and p["members_without_prices"] == {} and p["ended"] == {}
    assert res["price_cache"]["symbols"] == 30 and res["price_cache"]["unexplained_jumps"] == 0
    assert set(res["components"]) == set(specs.COMPONENTS) and set(res["unknown_components"]) == {
        "pe_percentile", "institutional_change", "promoter_change", "forensic_flags"}  # fmt: skip
    mom = res["components"]["mom_score"]["horizons"]
    # every month-end after the warm-up whose window ends inside the calendar; the warm-up months are counted
    assert mom["1"]["months"] == 131 - WARM - 1 and mom["12"]["months"] == 131 - WARM - 12
    assert mom["1"]["unknown_months"] == {"too_few_names": WARM}
    assert mom["12"]["mean"] > 0.1 and mom["12"]["interval"]["lo"] > 0  # the planted persistence in drifts
    assert res["families"]["A"]["size"] == 20 and res["families"]["B"]["size"] == 10
    for head in ("## Rank IC by horizon", "## Decay", "## Sub-periods", "## Quintiles, turnover and break-even cost",
                 "## Reading", "## What is unknown", "## Limits"):  # fmt: skip
        assert head in text, head
    assert "is never called an edge" in text and "`pe_percentile`" in text and "None" not in text
    again = _run("--data-dir", str(cache), "--out-dir", str(out))
    assert again.exit_code == 2 and "already run" in again.output
    forced = _run("--data-dir", str(cache), "--out-dir", str(out), "--overwrite", "--json")
    assert forced.exit_code == 0 and json.loads(forced.output) == res  # deterministic, to the last digit
    assert json.loads((out / "results.json").read_text()) == res


def test_the_outputs_hold_statistics_and_no_price(cache, tmp_path, made_up_index) -> None:
    res = study.execute(cache, tmp_path / "out")
    universe, _, _ = bt.load(cache)
    closes = {round(c, 6) for s in universe.values() for c in s.adj.close}
    numbers: set[float] = set()

    def walk(x) -> None:
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, float):
            numbers.add(round(x, 6))

    walk(res)
    assert (
        len(numbers) > 1000 and not numbers & closes
    )  # thousands of statistics, not one of the 86,100 closes
    assert not any(k in json.dumps(res) for k in ('"close"', '"prices"', '"returns"'))


def test_the_command_is_refused_before_any_read_when_the_registration_moved(
    cache, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(prereg, "HASH", "0" * 64)
    monkeypatch.setattr(bt, "load", lambda *_: pytest.fail("a price was read without a valid registration"))
    r = _run("--data-dir", str(cache), "--out-dir", str(tmp_path / "out"))
    assert r.exit_code == 2 and "registered hash" in r.output and not (tmp_path / "out").exists()


def test_the_command_is_refused_without_a_price_cache(tmp_path) -> None:
    r = _run("--data-dir", str(tmp_path / "nothing"), "--out-dir", str(tmp_path / "out"))
    assert (
        r.exit_code == 2
        and "no price cache" in r.output.replace("\n", " ")
        and not (tmp_path / "out").exists()
    )


def test_members_without_prices_leave_every_cell_unknown_never_zero(cache, tmp_path) -> None:
    """The REAL pinned membership over the generated cache: no Nifty 50 member has a price file there, so nothing can
    be computed, and the report must say so with the counts instead of printing zeros."""
    res = study.execute(cache, tmp_path / "out")
    assert res["panel"]["months_with_eligible_names"] == 0 and res["panel"]["eligible"] is None
    assert sum(res["panel"]["members_without_prices"].values()) >= 131 * 50
    assert res["families"]["A"]["known"] == 0 and res["families"]["A"]["detected"] == []
    for comp in res["components"].values():
        for h, c in comp["horizons"].items():
            assert c["months"] == 0 and c["mean"] is None and c["sign_flip"]["p"] is None and c["q"] is None
            assert (
                c["unknown"] == "only 0 evaluated months (needs 36)"
                and c["break_even"]["long_only"]["cost"] is None
            )
            assert c["unknown_months"] == {"too_few_names": 131 - int(h)}
        assert {n["label"] for n in comp["reading"]} == {"unknown"}
    text = (tmp_path / "out" / "RESULTS.md").read_text()
    assert "0 known" in text and "| unknown |" in text


def test_the_diagnostics_import_no_database_no_exchange_adapter_and_no_new_dependency() -> None:
    code = ("import sys, finresearch.factordiag.study, finresearch.factordiag.report, finresearch.factordiag.synth\n"
            "bad = [m for m in sys.modules if m.split('.')[0] in ('sqlalchemy', 'psycopg', 'pandas', 'scipy', "
            "'statsmodels') or m.startswith(('finresearch.db', 'finresearch.adapters', 'finresearch.portfolio'))]\n"
            "print(bad)")  # fmt: skip
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True
    )
    assert out.stdout.strip() == "[]", out.stdout
