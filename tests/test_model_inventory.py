"""#379: model inventory, evidence staleness (age and market-regime changes) and run provenance."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from finresearch.inventory import regimes


# --------------------------------------------------------------------------- staleness
def test_a_recent_run_after_every_regime_change_is_current():
    assert regimes.staleness(date(2026, 10, 12), date(2026, 10, 22)) == []


def test_a_run_older_than_max_age_is_stale_with_its_age():
    # 22-Oct-2026 − 6-Sep-2026 = 46 days (Sep has 30: 24 + 22)
    why = regimes.staleness(date(2026, 9, 6), date(2026, 10, 22))
    assert len(why) == 1 and "46 days old" in why[0] and "45" in why[0]
    assert regimes.staleness(date(2026, 9, 7), date(2026, 10, 22)) == []  # exactly 45 days: still current


def test_a_run_from_before_a_regime_change_is_stale_even_when_young():
    # NIFTY weekly expiry moved from Thursday to Tuesday from 1-Sep-2025
    why = regimes.staleness(date(2025, 8, 30), date(2025, 9, 10))
    assert len(why) == 1 and "Tuesday" in why[0] and "01-Sep-2025" in why[0]


def test_stt_changes_come_from_the_dated_charges_table():
    days = {c.day for c in regimes.regime_changes() if c.tag == "stt"}
    # fincalc/charges.py stt_* rows, dated since #382 from NSE's circulars: futures 0.01 % (FATAX/23500), option sale
    # 0.05 % (FATAX/32385), exercise STT on intrinsic value (FATAX/41919), Finance Act 2023 (FATAX/56235), Finance
    # (No. 2) Act 2024 (FATAX/63809), Finance Bill 2026 (memo); since #365 also the Finance Act 2006 (futures 0.017 %)
    # and 2008 (option sale on premium, exercise 0.125 %) rows; the unknown-period row of 1-Jan-2008 is not a change
    assert days == {date(2006, 6, 1), date(2008, 6, 1), date(2013, 6, 1), date(2016, 6, 1), date(2019, 9, 1), date(2023, 4, 1), date(2024, 10, 1),
                    date(2026, 4, 1)}  # fmt: skip
    # the older dates fall before any run this app can have made: a run after Oct-2024 is judged as before
    assert regimes.staleness(date(2024, 10, 5), date(2024, 10, 15)) == []
    why = regimes.staleness(date(2026, 3, 25), date(2026, 4, 10))
    assert len(why) == 1 and "STT" in why[0]


def test_weekly_expiry_cut_of_nov_2024_is_a_regime_change():
    why = regimes.staleness(date(2024, 11, 15), date(2024, 11, 25))
    assert len(why) == 1 and "20-Nov-2024" in why[0]


def test_an_unknown_run_date_is_stale_never_current():
    why = regimes.staleness(None, date(2026, 10, 22))
    assert why and "unknown" in why[0]


def test_run_day_reads_iso_and_datetimes_in_ist():
    # 20:00 UTC on 21-Oct is 01:30 IST on 22-Oct
    assert regimes.run_day("2026-10-21T20:00:00+00:00") == date(2026, 10, 22)
    assert regimes.run_day(datetime(2026, 10, 21, 20, 0, tzinfo=UTC)) == date(2026, 10, 22)
    assert regimes.run_day(None) is None and regimes.run_day("garbage") is None


# --------------------------------------------------------------------------- provenance
def _tree(root, files):
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)


def test_code_hash_is_sha256_over_sorted_path_and_bytes(tmp_path):
    import hashlib

    from finresearch.inventory import provenance as pv

    _tree(tmp_path, {"eng/b.py": b"y = 2\n", "eng/a.py": b"x = 1\n", "eng/__pycache__/a.cpython-312.pyc": b"\0",
                     "one.py": b"z = 3\n", "eng/notes.txt": b"not code"})  # fmt: skip
    h = hashlib.sha256()
    for rel, body in (("eng/a.py", b"x = 1\n"), ("eng/b.py", b"y = 2\n"), ("one.py", b"z = 3\n")):
        h.update(rel.encode() + b"\0" + hashlib.sha256(body).digest())
    assert pv.code_hash(tmp_path, ("eng", "one.py")) == h.hexdigest()
    (tmp_path / "eng/b.py").write_bytes(b"y = 3\n")  # editing engine code changes the hash
    assert pv.code_hash(tmp_path, ("eng", "one.py")) != h.hexdigest()


def test_a_missing_engine_path_is_an_error_not_a_smaller_hash(tmp_path):
    import pytest

    from finresearch.inventory import provenance as pv

    with pytest.raises(FileNotFoundError):
        pv.code_hash(tmp_path, ("nope",))


def test_engine_hash_binds_code_commit_and_dirty_flag():
    import hashlib

    from finresearch.inventory import provenance as pv

    want = hashlib.sha256(b"c0de|abc123|clean").hexdigest()
    assert pv.engine_hash("c0de", "abc123", False) == want
    assert pv.engine_hash("c0de", "abc123", True) != want
    assert pv.engine_hash("c0de", None, None) == hashlib.sha256(b"c0de|unknown|unknown").hexdigest()


def test_git_state_outside_a_repository_is_unknown(tmp_path):
    from finresearch.inventory import provenance as pv

    assert pv.git_state(tmp_path, ("x",)) == (None, None)


def test_the_live_engine_paths_exist_and_hash():
    from finresearch.inventory import provenance as pv

    assert len(pv.code_hash(pv.REPO, pv.ENGINE_PATHS)) == 64


def test_code_changed_since_the_run():
    from finresearch.inventory import provenance as pv

    now = pv.code_hash(pv.REPO, pv.ENGINE_PATHS)
    assert pv.code_changed({"code_hash": now}) is None
    assert "code changed since" in pv.code_changed({"code_hash": "0" * 64})
    assert "unknown" in pv.code_changed({})  # a run saved before #379 has no provenance: never "unchanged"


def test_data_hash_over_the_bhav_manifest_window(env):
    import hashlib
    import json

    from sqlalchemy import text

    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhavDay
    from finresearch.inventory import provenance as pv

    with session_scope() as s:
        s.execute(text("TRUNCATE fno_bhav_day"))
        s.add_all([FnoBhavDay(day=date(2021, 12, 31), status="loaded", rows_kept=5),  # before the window
                   FnoBhavDay(day=date(2022, 1, 3), status="loaded", rows_kept=100),
                   FnoBhavDay(day=date(2022, 1, 1), status="no_file", rows_kept=None)])  # fmt: skip
    rows = [["2022-01-01", "no_file", None], ["2022-01-03", "loaded", 100]]
    want = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
    with session_scope() as s:
        got = pv.data_hash(s, date(2022, 1, 1), None)
        assert got == {"data_hash": want, "data_days": 2, "data_from": "2022-01-01", "data_to": "2022-01-03"}
        s.execute(text("UPDATE fno_bhav_day SET rows_kept = 99 WHERE day = '2022-01-03'"))
        assert pv.data_hash(s, date(2022, 1, 1), None)["data_hash"] != want


def test_fno_backtest_run_saves_engine_and_data_hashes(env, monkeypatch):
    from typer.testing import CliRunner

    from finresearch.backtest import dbhistory, evaluate
    from finresearch.backtest import store as bstore
    from finresearch.cli import app
    from finresearch.db import session_scope
    from finresearch.inventory import provenance as pv

    rep = {"version": "t", "registration_hash": "h", "registered_hash": "h", "oos_evaluated": True, "source": "x",
           "variants_tried": 8, "stock_rule_symbol": None, "strategies": [],
           "windows": {"oos": ["2024-07-01", "2026-10-09"]}}  # fmt: skip
    monkeypatch.setattr(dbhistory, "top_stock", lambda h, a, b: None)
    monkeypatch.setattr(evaluate, "evaluate", lambda h, **kw: dict(rep))
    r = CliRunner().invoke(app, ["fno", "backtest", "run"])
    assert r.exit_code == 0, r.output
    with session_scope() as s:
        saved = bstore.run_json(s, bstore.latest_run(s))
    assert saved["code_hash"] == pv.code_hash(pv.REPO) and len(saved["engine_hash"]) == 64
    assert len(saved["data_hash"]) == 64 and "data_days" in saved and "git_dirty" in saved
    # second slice: the regime each tag was in on the run's (IST) day is stored with the run
    assert saved["regime_tags"] == regimes.regime_state(regimes.run_day(saved["created_at"]))


# --------------------------------------------------------------------------- registry
def test_registry_keys_are_unique_and_every_entry_is_complete():
    from finresearch.inventory import registry

    es = registry.entries()
    assert len({e.key for e in es}) == len(es)
    for e in es:
        assert e.purpose and e.intended_use and e.restrictions and e.outcome_metric and e.code, e.key
        assert e.tier in ("A", "B", "C") and all(m.strip() for m in e.methods), e.key


def test_the_issue_s_models_are_all_in_the_inventory():
    from finresearch.backtest import specs
    from finresearch.inventory import registry

    want = {"fno_strategy_gate", "fno_tradeplan_verdict", "direction_logistic", "direction_implied", "ipo_base_rate",
            "ipo_blend", "stock_composite", "risk_margin_approx", "risk_lognormal_pop", "risk_drawdown_mc",
            "risk_var", "risk_stress", *(f"recipe:{s.key}" for s in specs.SPECS)}  # fmt: skip
    assert want <= {e.key for e in registry.entries()}


def test_status_is_validated_only_with_current_dates():
    from dataclasses import replace

    from finresearch.inventory import registry

    base = registry.entry("fno_strategy_gate")
    ok = replace(base, declared="validated", validated_on=date(2026, 9, 1), valid_until=date(2026, 12, 1))
    # a tier-A entry also needs a filed red-team review since the second slice (behaviour changed on purpose)
    assert ok.status(date(2026, 10, 22), reviews=1)[0] == "validated"
    st, why = ok.status(date(2026, 12, 2), reviews=1)  # expired evidence reads unvalidated, never fine
    assert st == "unvalidated" and "expired on 01-Dec-2026" in why
    assert replace(base, declared="validated").status(date(2026, 10, 22))[0] == "unvalidated"  # no dates
    assert (
        registry.entry("direction_logistic").status(date(2030, 1, 1))[0] == "failed"
    )  # time never rescues a fail


def test_no_entry_is_declared_validated_without_its_evidence():
    from finresearch.inventory import registry

    for e in registry.entries():
        st, _ = e.status(date(2026, 10, 22))
        assert st in ("unvalidated", "failed"), (
            f"{e.key}: nothing has passed a validation yet (#379, 22-Oct-2026)"
        )


def test_unknown_methods_map_to_nothing():
    from finresearch.inventory import registry

    assert registry.entry_for_method("a brand new method nobody registered") is None
    assert registry.entry_for_method("rule: QIB above 2x").key == "ipo_rules"


def test_every_entry_s_code_and_cited_write_up_exist():
    """A stale path in the inventory would cite evidence nobody can open (#379 second slice)."""
    import re

    from finresearch.inventory import registry

    root = Path(__file__).resolve().parents[1]
    for e in registry.entries():
        for c in e.code:
            assert (root / "src" / "finresearch" / c).exists(), f"{e.key}: code {c}"
        for p in re.findall(r"evals/experiments/[\w/]+\.md", e.evidence):
            assert (root / p).exists(), f"{e.key}: evidence {p}"


def test_the_models_merged_after_the_first_slice_are_in_the_inventory():
    from finresearch.inventory import registry

    want = {"horse_race", "hr_p_rw_bus", "hr_p_rn_b76", "hr_p_rn_svi", "hr_p_rw_tail", "hr_p_path", "joint_risk",
            "risk_alerts", "risk_var_ledger", "vol_race", "vrp_collectable", "stock_option_vrp", "optimiser",
            "goal_planner", "factor_exposures", "brinson_attribution", "book_beta"}  # fmt: skip
    assert want <= {e.key for e in registry.entries()}


# --------------------------------------------------------------------------- regime tags per run (#379 second slice)
def test_regime_state_names_the_regime_each_tag_was_in_on_a_day():
    st = regimes.regime_state(date(2025, 8, 29))
    assert st["expiry_weekday"] == "before 2025-09-01" and st["weekly_expiries"] == "2024-11-20"
    after = regimes.regime_state(date(2025, 9, 2))
    assert after["expiry_weekday"] == "2025-09-01" and after["weekly_expiries"] == "2024-11-20"
    assert set(st) == {"expiry_weekday", "weekly_expiries", "lot_size", "stt"}
    assert st["lot_size"] == "2024-11-20"  # the SEBI 2024 circular changed lot sizes too


def test_stored_regime_tags_that_differ_from_today_s_make_the_run_stale_by_tag():
    run_tags = regimes.regime_state(date(2025, 8, 29))
    why = regimes.staleness(
        date(2026, 10, 20), date(2026, 10, 22), tags=run_tags
    )  # young, but tagged pre-Tuesday
    assert any("expiry_weekday" in w for w in why)
    now_tags = regimes.regime_state(date(2026, 10, 22))
    assert regimes.staleness(date(2026, 10, 20), date(2026, 10, 22), tags=now_tags) == []


def test_unreadable_stored_regime_tags_are_stale_never_current():
    why = regimes.staleness(date(2026, 10, 20), date(2026, 10, 22), tags={"expiry_weekday": "?"})
    assert why and all("regime" in w for w in why)


def test_a_correctly_dated_run_gets_one_reason_per_regime_change_not_two():
    run = date(2025, 8, 20)
    why = regimes.staleness(run, date(2025, 9, 10), tags=regimes.regime_state(run))
    assert [w for w in why if "01-Sep-2025" in w or "expiry_weekday" in w] == [
        next(w for w in why if "01-Sep-2025" in w)
    ]  # the date reason covers it; the tag reason is only the safety net for a mis-dated run
