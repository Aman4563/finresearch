"""#382 (P2.7a): the next-print fill variant of the backtester. A decision made at day t's close fills at the next
session's open (legs that traded that session) or its close (`next_open`), or at the next session's close
(`next_close`), with the registered slippage tiers on the fill session's contracts. Exits lag the same way. The
registered `same_print` engine is unchanged. Synthetic history only (tests/fno_synth.py); expected fills are read
from the history's own quotes and worked out with the tier rule written out here, not by calling the engine."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date

import fno_synth as fs
import pytest

from finresearch.backtest import engine, specs

START, END = date(2022, 1, 3), date(2022, 12, 30)
TIERS = (
    (1000, 0.01),
    (100, 0.02),
    (10, 0.04),
    (1, 0.08),
    (0, 0.15),
)  # specs.Book.slippage_tiers, written out


def fill(ref: float, side: str, contracts: int, mult: float = 1.0) -> float:
    pct = next(p for n, p in TIERS if contracts >= n)
    s = max(pct * ref, 0.10) * mult
    return max(0.05, round((ref + s if side == "buy" else ref - s) + 1e-9, 2))


def gapped(seed: int = 7) -> tuple[dict[date, float], dict[date, float]]:
    """A path whose every open sits 0.6 % above the previous close (so open and close prices differ)."""
    px = fs.path(START, END, seed=seed)
    days = sorted(px)
    return px, {d: px[days[i - 1]] * 1.006 if i else px[d] for i, d in enumerate(days)}


def nifty_specs() -> list[specs.Spec]:
    return [replace(s, iv_pct_min=None) for s in specs.SPECS if s.underlying == "NIFTY"]


def run(fill_mode: str, h=None) -> dict:
    px, op = gapped()
    h = h or fs.history(START, END, prices=px, opens=op)
    return engine.run(nifty_specs(), h, date(2022, 2, 1), date(2022, 11, 30), warmup_days=40, fill=fill_mode)


def test_same_print_is_the_registered_engine_unchanged():
    px, op = gapped()
    h = fs.history(START, END, prices=px, opens=op)
    a = engine.run(nifty_specs(), h, date(2022, 2, 1), date(2022, 11, 30), warmup_days=40)
    b = engine.run(nifty_specs(), h, date(2022, 2, 1), date(2022, 11, 30), warmup_days=40, fill="same_print")
    assert a == b
    assert all(
        "decision_day" not in t for r in a.values() for t in r["trades"]
    )  # no new keys in the stored trades
    with pytest.raises(ValueError, match="fill"):
        engine.run(nifty_specs(), h, date(2022, 2, 1), date(2022, 11, 30), warmup_days=40, fill="next_tick")


@pytest.mark.parametrize("mode", ["next_open", "next_close"])
def test_entries_decided_at_t_fill_at_the_next_print(mode):
    px, op = gapped()
    h = fs.history(START, END, prices=px, opens=op)
    days = sorted(px)
    nxt = {d: days[i + 1] for i, d in enumerate(days[:-1])}
    res = run(mode, h)
    n = 0
    for r in res.values():
        for t in r["trades"]:
            dec, ent = date.fromisoformat(t["decision_day"]), date.fromisoformat(t["entry_day"])
            assert t["fill"] == mode and ent == nxt[dec]  # filled on the session after the decision
            x = h.days["NIFTY"][ent]
            for leg in t["legs"]:
                q = x.options[(date.fromisoformat(t["expiry"]), leg["right"], leg["strike"])]
                ref = q.open if mode == "next_open" and q.traded and q.open else q.ref
                assert leg["entry"] == pytest.approx(fill(ref, leg["side"], q.contracts if q.traded else 0))
                n += 1
    assert n > 20


def test_next_print_decisions_are_the_same_print_decisions():
    """The decision is unchanged by the lag: the first entry of every spec is decided on the same day with the same
    strikes and lots as the registered engine's entry on that day (later ones may differ as P&L paths differ)."""
    same, nxt = run("same_print"), run("next_open")
    for key in same:
        a, b = same[key]["trades"][0], nxt[key]["trades"][0]
        assert b["decision_day"] == a["entry_day"]
        assert [(x["right"], x["strike"], x["side"]) for x in a["legs"]] == [
            (x["right"], x["strike"], x["side"]) for x in b["legs"]
        ]
        assert a["lots"] == b["lots"]


def test_exits_lag_and_sellers_are_out_before_expiry_day():
    px, op = gapped()
    h = fs.history(START, END, prices=px, opens=op)
    days = sorted(px)
    nxt = {d: days[i + 1] for i, d in enumerate(days[:-1])}
    res = run("next_open", h)
    lagged = 0
    for key, r in res.items():
        hold = specs.by_key(key).hold_to_expiry
        for t in r["trades"]:
            assert t["net"] == pytest.approx(
                t["gross"] - t["entry_costs"] - t["exit_costs"] - t["exercise_stt"], abs=0.02
            )
            if t["reason"] == "expiry":
                assert hold and t["exit_day"] <= t["expiry"]
                continue
            dec, ex = date.fromisoformat(t["exit_decision_day"]), date.fromisoformat(t["exit_day"])
            assert ex == nxt[dec]
            assert ex < date.fromisoformat(
                t["expiry"]
            )  # out on the last session before expiry day at the latest
            x = h.days["NIFTY"][ex]
            for leg in t["legs"]:
                q = x.options.get((date.fromisoformat(t["expiry"]), leg["right"], leg["strike"]))
                if q is None:  # no row that session: closed at its last mark, the widest tier
                    continue
                ref = q.open if q.traded and q.open else q.ref
                close_side = "sell" if leg["side"] == "buy" else "buy"
                assert leg["exit"] == pytest.approx(fill(ref, close_side, q.contracts if q.traded else 0))
            lagged += 1
    assert lagged > 10


def test_an_untraded_leg_fills_at_the_next_close_with_the_widest_tier():
    px, op = gapped()
    h = fs.history(START, END, prices=px, opens=op)
    first = run("next_open", h)["nifty_pcs_m"]["trades"][0]
    ent, e = date.fromisoformat(first["entry_day"]), date.fromisoformat(first["expiry"])
    wing = next(x for x in first["legs"] if x["side"] == "buy")
    x = h.days["NIFTY"][ent]
    q = x.options[(e, "put", wing["strike"])]
    x.options[(e, "put", wing["strike"])] = replace(
        q, close=None, contracts=0, open=None, high=None, low=None
    )
    again = run("next_open", h)["nifty_pcs_m"]["trades"][0]
    leg = next(x for x in again["legs"] if x["side"] == "buy")
    assert leg["entry"] == pytest.approx(fill(q.settle, "buy", 0))  # NSE's settle, 15 % tier


def test_a_missing_leg_at_the_fill_session_is_a_counted_skip():
    px, op = gapped()
    h = fs.history(START, END, prices=px, opens=op)
    first = run("next_open", h)["nifty_pcs_m"]["trades"][0]
    ent, e = date.fromisoformat(first["entry_day"]), date.fromisoformat(first["expiry"])
    short = next(x for x in first["legs"] if x["side"] == "sell")
    del h.days["NIFTY"][ent].options[(e, "put", short["strike"])]
    r = run("next_open", h)["nifty_pcs_m"]
    assert r["skips"].get("no price for a leg at the fill session") == 1
    assert r["trades"][0]["decision_day"] != first["decision_day"]


def test_next_print_never_reads_ahead_of_its_fill():
    """Two histories identical to T, then one crashes 25 %: every entry decided by T-1 and filled by T is identical
    (exits after T differ, as they should)."""
    px, op = gapped(seed=11)
    cut = date(2022, 7, 15)
    crash = {d: (s if d <= cut else s * 0.75) for d, s in px.items()}
    a = run("next_open", fs.history(START, END, prices=px, opens=op))
    b = run("next_open", fs.history(START, END, prices=crash, opens={d: (s if d <= cut else s * 0.75)
                                                                      for d, s in op.items()}))  # fmt: skip
    for key in a:
        upto = lambda r: [(t["decision_day"], t["entry_day"], [(x["strike"], x["entry"]) for x in t["legs"]],  # noqa: E731
                           t["lots"], t["entry_costs"]) for t in r["trades"] if t["entry_day"] <= cut.isoformat()]  # fmt: skip
        assert upto(a[key]) == upto(b[key])


# --------------------------------------------------------------------------- the paired comparison (compare.py)
def tr(day: str, expiry: str, net: float, reason: str = "profit_take", decision: str | None = None) -> dict:
    t = {"entry_day": day, "expiry": expiry, "net": net, "reason": reason}
    if decision:
        t.update(decision_day=decision, entry_day="2099-01-01", fill="next_open")  # entry day is the fill's
    return t


def test_paired_difference_clustered_t_and_the_sign_flip():
    from finresearch.backtest import compare

    same = [tr("2024-07-01", "2024-07-25", 100, "stop"), tr("2024-07-08", "2024-07-25", 50),
            tr("2024-08-01", "2024-08-29", 30), tr("2024-08-05", "2024-08-29", -20),
            tr("2024-06-28", "2024-07-25", 999)]  # before the window: left out  # fmt: skip
    nxt = [tr("", "2024-07-25", 40, "stop", "2024-07-01"), tr("", "2024-07-25", 10, decision="2024-07-08"),
           tr("", "2024-08-29", 50, "stop", "2024-08-01"), tr("", "2024-08-29", -100, decision="2024-08-12")]  # fmt: skip
    got = compare.paired(same, nxt, date(2024, 7, 1), date(2024, 9, 30))
    # D = 60, 40, -20 on expiries (Jul, Jul, Aug); mean 80/3. Cluster sums of (D - mean): +46.67, -46.67;
    # Var = G/(G-1) x sum of squares / n^2 = 2 x 2 x 46.67^2 / 9
    m = 80 / 3
    var = 2 * ((60 - m + 40 - m) ** 2 + (-20 - m) ** 2) / 9
    t = m / var**0.5
    assert got["paired"] == 3 and got["unpaired_same"] == 1 and got["unpaired_next"] == 1
    assert got["mean_d"] == pytest.approx(m, abs=0.01) and got["t_d"] == pytest.approx(t, abs=1e-4)
    assert got["p_one_sided"] == pytest.approx(0.5 * math.erfc(t / 2**0.5), abs=1e-4)
    assert (got["expectancy_same"], got["expectancy_next"], got["expectancy_change"]) == (40.0, 0.0, -40.0)
    assert got["execution_sensitive"] is True  # +40 a trade became 0: the sign is gone
    assert (got["stops_same"], got["stops_next"]) == (1, 2)
    # no trades on one side: the label is unknown, never "not sensitive"
    assert compare.paired(same, [], date(2024, 7, 1), date(2024, 9, 30))["execution_sensitive"] is None


def test_compare_reports_refuses_mismatched_reports():
    from finresearch.backtest import compare

    rep = {
        "registration_hash": "h",
        "windows": {"is": ["2022-01-01", "2024-06-30"], "oos": None},
        "strategies": [],
    }
    with pytest.raises(ValueError, match="same-print"):
        compare.compare_reports({**rep, "fill": "next_open"}, {**rep, "fill": "next_open"})
    with pytest.raises(ValueError, match="registration"):
        compare.compare_reports(
            {**rep, "fill": "same_print"}, {**rep, "registration_hash": "x", "fill": "next_open"}
        )


def test_cli_compare_prints_each_spec(tmp_path):
    import json

    from typer.testing import CliRunner

    from finresearch.backtest import evaluate
    from finresearch.cli import app

    mem = fs.history(date(2023, 6, 1), date(2025, 1, 31))
    for name, mode in (("same", "same_print"), ("next", "next_open")):
        rep = evaluate.evaluate(mem, stock=None, source="synthetic", fill=mode)
        (tmp_path / f"{name}.json").write_text(json.dumps(rep, default=str))
    out = CliRunner().invoke(app, ["fno", "backtest", "compare", str(tmp_path / "same.json"),
                                   str(tmp_path / "next.json")], env={"COLUMNS": "250"})  # fmt: skip
    assert out.exit_code == 0, out.output
    assert "nifty_dcs_m" in out.output and "execution-sensitive" in out.output
    bad = CliRunner().invoke(app, ["fno", "backtest", "compare", str(tmp_path / "next.json"),
                                   str(tmp_path / "same.json")])  # fmt: skip
    assert bad.exit_code != 0
