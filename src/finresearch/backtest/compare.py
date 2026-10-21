"""Same-print against next-print fills (#382, P2.7a): how much of a spec's result is the fill assumption.

Both runs replay the same frozen spec; the next-print run decides at the same closes and fills one session later
(engine.run(fill=...)). Trades are paired by the close the entry was decided at (a same-print trade's entry day, a
next-print trade's `decision_day`); after the first different exit the two runs can enter on different days, and
unpaired trades are counted, never dropped silently.

D = net(same print) − net(next print) per paired trade, grouped by the same-print trade's expiry. The one-sided test
of "same-print fills overstate ₹ per trade" is the expiry-clustered t of mean D (metrics.clustered_t) against the
normal, p = 1 − Φ(t). A spec whose expectancy sign differs between the two runs is "execution-sensitive" (expectancy
> 0 in one and ≤ 0 in the other); an expectancy that cannot be computed leaves the label unknown (None), never False.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from finresearch.backtest import metrics

FILL_KEY = "fill"


def decided(t: dict[str, Any]) -> str:
    """The close an entry was decided at: `decision_day` for a next-print trade, else the entry day."""
    return t.get("decision_day") or t["entry_day"]


def _in(trades: Sequence[dict[str, Any]], lo: date, hi: date) -> list[dict[str, Any]]:
    return [t for t in trades if lo <= date.fromisoformat(decided(t)) <= hi]


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _pairs(a: Sequence[dict[str, Any]], b: Sequence[dict[str, Any]]) -> list[tuple[dict, dict]]:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for t in b:
        by_day.setdefault(decided(t), []).append(t)
    out = []
    for t in a:
        got = by_day.get(decided(t))
        if got:
            out.append((t, got.pop(0)))
    return out


def _d_test(d: Sequence[float], clusters: Sequence[Any]) -> dict[str, Any]:
    """Mean D, its expiry-clustered t and the one-sided p of "same print overstates" (t > 0)."""
    t_stat = metrics.clustered_t(d, clusters)
    p = None if t_stat is None else 1 - metrics.N01.cdf(t_stat)
    return {"mean_d": metrics._r(_mean(d), 2), "t_d": metrics._r(t_stat, 4), "p_one_sided": metrics._r(p, 4),
            "expiries": len(set(clusters))}  # fmt: skip


def paired(
    same: Sequence[dict[str, Any]], nxt: Sequence[dict[str, Any]], lo: date, hi: date
) -> dict[str, Any]:
    """The comparison of one spec's trades decided from `lo` to `hi` (module docstring)."""
    a, b = _in(same, lo, hi), _in(nxt, lo, hi)
    pairs = _pairs(a, b)
    d, clusters = [x["net"] - y["net"] for x, y in pairs], [x["expiry"] for x, _ in pairs]
    e_same, e_next = _mean([t["net"] for t in a]), _mean([t["net"] for t in b])
    sensitive = None if e_same is None or e_next is None else (e_same > 0) != (e_next > 0)
    stops = lambda ts: sum(t["reason"] == "stop" for t in ts)  # noqa: E731
    r = metrics._r
    return {
        "from": lo.isoformat(), "to": hi.isoformat(),
        "trades_same": len(a), "trades_next": len(b), "paired": len(pairs),
        "unpaired_same": len(a) - len(pairs), "unpaired_next": len(b) - len(pairs),
        "expectancy_same": r(e_same, 2), "expectancy_next": r(e_next, 2),
        "expectancy_change": r(None if e_same is None or e_next is None else e_next - e_same, 2),
        **_d_test(d, clusters), "stops_same": stops(a), "stops_next": stops(b), "execution_sensitive": sensitive,
    }  # fmt: skip


def compare_reports(same: dict[str, Any], nxt: dict[str, Any]) -> list[dict[str, Any]]:
    """Per spec, the paired comparison over the out-of-sample window (else the in-sample one) of two evaluate()
    reports: one same-print, one next-print, of the same registration and windows (refused otherwise)."""
    if same.get(FILL_KEY, "same_print") != "same_print" or nxt.get(FILL_KEY, "same_print") == "same_print":
        raise ValueError("compare a same-print report with a next-print report")
    if same["registration_hash"] != nxt["registration_hash"] or same["windows"] != nxt["windows"]:
        raise ValueError("the two reports differ in registration or windows: not comparable")
    lo, hi = same["windows"]["oos"] or same["windows"]["is"]
    other = {r["key"]: r for r in nxt["strategies"]}
    out = []
    for r in same["strategies"]:
        o = other.get(r["key"])
        if "trades" not in r or o is None or "trades" not in o:
            out.append({"key": r["key"], "name": r["name"], "status": "not compared",
                        "reason": "no trades in one of the reports (no data, or a stored summary without trades)"})  # fmt: skip
            continue
        row = paired(r["trades"], o["trades"], date.fromisoformat(lo), date.fromisoformat(hi))
        out.append({"key": r["key"], "name": r["name"], "fill": nxt[FILL_KEY], **row})
    return out


SELLERS = (
    "nifty_pcs_m",
    "nifty_ccs_m",
    "banknifty_ic_m",
    "nifty_ic_w",
)  # the IV-filtered sellers of specs.py
TOP_IV = 2 / 3  # metrics.regime's "high" third of the IV percentile


def seller_tests(same: dict[str, Any], nxt: dict[str, Any]) -> dict[str, Any]:
    """PREREG (evals/experiments/execution_realism) H1 pooled over SELLERS, clusters = (underlying, expiry), and H1b:
    the pairs whose same-print entry IV percentile is in the top third (an unknown percentile is left out)."""
    compare_reports(same, nxt)  # the same refusals
    lo, hi = (date.fromisoformat(x) for x in (same["windows"]["oos"] or same["windows"]["is"]))
    other = {r["key"]: r for r in nxt["strategies"]}
    pairs: list[tuple[dict, dict, tuple[Any, str]]] = []
    for r in same["strategies"]:
        o = other.get(r["key"])
        if r["key"] in SELLERS and "trades" in r and o is not None and "trades" in o:
            pairs += [(x, y, (r.get("underlying"), x["expiry"]))
                      for x, y in _pairs(_in(r["trades"], lo, hi), _in(o["trades"], lo, hi))]  # fmt: skip

    def test(ps: list[tuple[dict, dict, tuple[Any, str]]]) -> dict[str, Any]:
        return {"paired": len(ps), **_d_test([x["net"] - y["net"] for x, y, _ in ps], [c for *_, c in ps])}

    top = [p for p in pairs if p[0].get("iv_pct") is not None and p[0]["iv_pct"] >= TOP_IV]
    return {"specs": [k for k in SELLERS if k in other], "sellers": test(pairs), "sellers_top_iv": test(top)}
