"""Criteria v2 of the F&O backtest (#381, P2.6): a NEW registered criteria variant, judged as a SHADOW beside the
registered criteria of specs.py. It never sets a strategy's status; `adopted` stays False until #380 (P2.5: a
sealed holdout and every trial logged) exists and the DSR-rule simulation has run on the real out-of-sample seller
P&L (evals/experiments/fno_backtest/ADDENDUM.md). Its hash (`registration_hash()`) is recorded in prereg_v2.py and
covers v1's registered hash, so neither can change silently.

What changes against v1, and in which direction (ADDENDUM.md states the same):
- P&L in excess of the yield on the cash part of the margin (inference.excess_net): STRICTER.
- t-statistic: two-way clustered by entry month × expiry (CGM 2011) instead of expiry only: not monotone (usually
  wider; equal to v1 when every expiry has one entry month).
- CI: stationary-bootstrap BCa over expiries in time order instead of an i.i.d. percentile bootstrap of expiries:
  not monotone.
- Drawdown on peak equity instead of on starting capital: MORE PERMISSIVE whenever the peak is above the capital.
- DSR: V = `dsr_rule` (max(V_null, V_cand) as the issue's Maths line registers it) and N = the trials whose SR enters
  V (6 on the published run) instead of VARIANTS_TRIED = 8: N alone is MORE PERMISSIVE; max(V_null, ·) only ever
  raises V (stricter), so on the published run V = V_cand and the bar falls only through N.
- Reported, not judged: the Lo (2002) annualised Sharpe and the posterior ('expected true') Sharpe under N(0, τ²).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from finresearch.backtest import inference, metrics, prereg, specs


@dataclass(frozen=True)
class CriteriaV2:
    min_oos_trades: int = 30
    min_t_stat: float = 2.0  # two-way clustered (entry month × expiry) t of the excess expectancy
    bootstrap_ci: float = 0.95  # stationary-bootstrap BCa CI of the excess expectancy must lie above 0
    bootstrap_draws: int = 5000
    bootstrap_seed: int = 381
    max_drawdown_peak_pct: float = 20.0  # of the running peak of the daily marked equity
    dsr_rule: str = (
        "max"  # inference.DSR_RULES; see the ADDENDUM for the V_null / V_cand contradiction in #381
    )
    min_deflated_sharpe: float = 0.95
    in_sample_expectancy_positive: bool = True
    stress_slippage_mult: float = 2.0  # as v1
    collateral_rate: float = (
        0.065  # [U] a year: the book's Black-76 rate (Book.rate), not a dated T-bill series
    )
    cash_share: float = 0.5  # NSE Clearing: cash >= 50 % of liquid assets; assumed to earn nothing [U]
    prior_tau: float = 0.5  # sd of the skeptical N(0, τ²) prior on the true annual Sharpe
    lo_q: int = 252  # sessions a year for Lo's η(q)
    adopted: bool = False


CRITERIA_V2 = CriteriaV2()
SHADOW_NOTE = ("Criteria v2 (#381) are a registered shadow, not adopted: they never change the status above. "
               "Adoption needs #380 (P2.5) and the DSR-rule simulation on real out-of-sample P&L.")  # fmt: skip


def registration(crit: CriteriaV2 = CRITERIA_V2) -> dict[str, Any]:
    return {
        "criteria_v2": asdict(crit),
        "base_registration": prereg.HASH,
        "dsr_rules": list(inference.DSR_RULES),
    }


def registration_hash(crit: CriteriaV2 = CRITERIA_V2) -> str:
    return hashlib.sha256(specs.canonical(registration(crit)).encode()).hexdigest()


def trade_part(trades: list[dict[str, Any]], crit: CriteriaV2 = CRITERIA_V2) -> dict[str, Any]:
    """The trade-level v2 statistics of one window on the P&L in excess of the collateral yield. Any trade with an
    unknown margin makes the excess, and so every statistic on it, unknown."""
    ex = [inference.excess_net(t, rate=crit.collateral_rate, cash_share=crit.cash_share) for t in trades]
    out: dict[str, Any] = {"trades": len(trades), "excess_expectancy": None, "t_twoway": None,
                           "t_fallback": None, "ci_low": None, "ci_high": None, "ci_block": None}  # fmt: skip
    if not trades or any(x is None for x in ex):
        return out
    xs = [float(x) for x in ex]  # type: ignore[arg-type]
    expiry = [t["expiry"] for t in trades]
    month = [t["entry_day"][:7] for t in trades]
    tw = inference.twoway_clustered_t(xs, month, expiry)
    ci = inference.cluster_bca(xs, expiry, crit.bootstrap_draws, crit.bootstrap_seed, crit.bootstrap_ci)
    out.update(excess_expectancy=sum(xs) / len(xs), t_twoway=tw["t"], t_fallback=tw["fallback"], ci_low=ci["lo"],
               ci_high=ci["hi"], ci_block=ci["block"])  # fmt: skip
    return out


def equity_part(equity: list[tuple[date, float]], w_oos: dict[str, Any], crit: CriteriaV2 = CRITERIA_V2
                ) -> dict[str, Any]:  # fmt: skip
    lo = inference.lo_sharpe(metrics.daily_returns(equity), q=crit.lo_q)
    post = inference.posterior_sharpe(w_oos.get("daily_sr"), (w_oos.get("days") or 0) - 1, w_oos.get("skew"),
                                      w_oos.get("kurtosis"), tau=crit.prior_tau)  # fmt: skip
    return {"max_drawdown_peak_pct": inference.drawdown_peak_pct(equity), "lo_sharpe": lo["sharpe"],
            "lo_eta": lo["eta"], "lo_max_lag": lo["max_lag"], "posterior_sharpe": post["posterior"],
            "posterior_shrink": post["shrink"]}  # fmt: skip


def judge(part: dict[str, Any], w_is: dict[str, Any], w_stress: dict[str, Any], trial_srs: list[float],
          w_oos: dict[str, Any], crit: CriteriaV2 = CRITERIA_V2) -> dict[str, Any]:  # fmt: skip
    """The shadow verdict of one strategy from its `part` (trade_part | equity_part of the OOS window)."""

    def c(name: str, value: Any, ok: bool | None, rule: str) -> dict[str, Any]:
        return {"criterion": name, "value": metrics._r(value, 4) if isinstance(value, float) else value,
                "pass": bool(ok), "rule": rule}  # fmt: skip

    dsr = inference.dsr_v2(w_oos, trial_srs, crit.dsr_rule)
    e, t, lo, dd = part["excess_expectancy"], part["t_twoway"], part["ci_low"], part["max_drawdown_peak_pct"]
    checks = [
        c("OOS trades", part["trades"], part["trades"] >= crit.min_oos_trades, f">= {crit.min_oos_trades}"),
        c("OOS expectancy after costs and collateral yield (₹/trade)", e, e is not None and e > 0, "> 0"),
        c("OOS t-stat (entry month × expiry)", t, t is not None and t >= crit.min_t_stat, f">= {crit.min_t_stat:g}"),
        c(f"OOS {crit.bootstrap_ci:.0%} stationary-bootstrap BCa CI low (₹/trade)", lo, lo is not None and lo > 0,
          "> 0"),
        c("OOS max drawdown (% of peak)", dd, dd is not None and dd <= crit.max_drawdown_peak_pct,
          f"<= {crit.max_drawdown_peak_pct:g}"),
        c("OOS deflated Sharpe (v2)", dsr["dsr"], dsr["dsr"] is not None and dsr["dsr"] >= crit.min_deflated_sharpe,
          f">= {crit.min_deflated_sharpe:g} (V = {crit.dsr_rule}, N = {dsr['n_trials']} trials with a known SR)"),
        c("In-sample expectancy (₹/trade)", w_is["expectancy"],
          w_is["expectancy"] is not None and w_is["expectancy"] > 0, "> 0"),
        c(f"OOS expectancy with slippage × {crit.stress_slippage_mult:g}", w_stress["expectancy"],
          w_stress["expectancy"] is not None and w_stress["expectancy"] > 0, "> 0"),
    ]  # fmt: skip
    failed = [x["criterion"] for x in checks if not x["pass"]]
    return {"status": "shadow_not_adopted", "verdict": "would_fail" if failed else "would_pass",
            "reasons": [f"fails: {x}" for x in failed], "criteria": checks, "registration_hash": registration_hash(crit),
            "dsr": {k: metrics._r(v, 10) if isinstance(v, float) else v for k, v in dsr.items()},
            "stats": {k: metrics._r(v, 4) if isinstance(v, float) else v for k, v in part.items()},
            "note": SHADOW_NOTE}  # fmt: skip
