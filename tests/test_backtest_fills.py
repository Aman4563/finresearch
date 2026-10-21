"""#382 (P2.7a): the next-print fill variant of the backtester. A decision made at day t's close fills at the next
session's open (legs that traded that session) or its close (`next_open`), or at the next session's close
(`next_close`), with the registered slippage tiers on the fill session's contracts. Exits lag the same way. The
registered `same_print` engine is unchanged. Synthetic history only (tests/fno_synth.py); expected fills are read
from the history's own quotes and worked out with the tier rule written out here, not by calling the engine."""

from __future__ import annotations

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
