"""The VRP study's report from prepared inputs (P3.6, #389; evals/experiments/vrp_collectable/PREREG.md). Pure: the
inputs come from vrp/data.py. Every figure is split by period (vault 2008-2021, seen 2022-2026) and the gate by era.
The outcome is reported, never adopted: nothing here changes a card, a spec or the evidence tag."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date

from finresearch.vrp import collect as co
from finresearch.vrp import study as st

PERIODS = {"vault": (date(2008, 1, 1), date(2021, 12, 31)), "seen": (date(2022, 1, 1), date(2026, 12, 31))}
ERAS = {"2008-2015": (date(2008, 1, 1), date(2015, 12, 31)), "2016-2021": (date(2016, 1, 1), date(2021, 12, 31)),
        "2022-2026": (date(2022, 1, 1), date(2026, 12, 31))}  # fmt: skip
FRACTIONAL_LABEL = "academic benchmark: not collectable at 1-2 lots"
VERDICT = ("Reported, not adopted (#389): no card, spec or evidence tag changes. A VRP-conditioned seller variant "
           "may be registered only if the after-cost P&L of the form it would trade is positive in BOTH periods "
           "(vault vrp.md); a positive log ratio, vol-unit gap or share of IV > RV never qualifies.")  # fmt: skip


@dataclass(frozen=True)
class Inputs:
    days: Sequence[st.Day]
    starts: set[date]  # the monthly expiries that are stored sessions
    data_end: date
    straddles: Sequence[co.Trade]
    puts: Sequence[co.Trade]
    z_mf: Mapping[
        date, float | None
    ]  # window start -> ln(S_T / F_t) / sqrt(sigma^2_near T) of the near expiry
    z_atm: Mapping[date, float | None]  # the same with the near expiry's ATM IV


def _in(d: date, rng: tuple[date, date]) -> bool:
    return rng[0] <= d <= rng[1]


def collect_block(trades: Sequence[co.Trade], hedges: Sequence[str], overrides: Mapping | None, *, draws: int,
                  seed: int) -> dict:  # fmt: skip
    out = {}
    for h in hedges:
        s = co.summarise([co.pnl(t, h, overrides=overrides) for t in trades], draws, seed)
        out[h] = {**s, "label": FRACTIONAL_LABEL} if h == "fractional" else s
    return out


def report(x: Inputs, *, draws: int = co.BOOTSTRAP_DRAWS, seed: int = co.BOOTSTRAP_SEED) -> dict:
    ws = st.windows(x.days, x.starts, x.data_end)
    flat_days = [replace(d, mf30=d.mf30_flat, mf30_reason=None if d.mf30_flat else "flat extension unknown")
                 for d in x.days]  # fmt: skip
    wf = st.windows(flat_days, x.starts, x.data_end)
    today = co.collectable_today_overrides()
    out: dict = {"gate": {}, "periods": {}, "verdict": VERDICT}
    for era, rng in ERAS.items():
        days = [d for d in x.days if _in(d.day, rng)]
        if days:
            out["gate"][era] = st.gate(days)
    for name, rng in PERIODS.items():
        w = [v for v in ws if _in(v.start, rng)]
        if not w:
            continue
        sd = [t for t in x.straddles if _in(t.entry, rng)]
        pt = [t for t in x.puts if _in(t.entry, rng)]
        out["periods"][name] = {
            "windows": len(w), "primary": st.primary(w), "terciles": st.terciles(w),
            "flat_sensitivity": st.primary([v for v in wf if _in(v.start, rng)]),
            "collect": {"dated": {"straddle": collect_block(sd, co.HEDGES, None, draws=draws, seed=seed),
                                  "put25": collect_block(pt, ("none",), None, draws=draws, seed=seed)},
                        "today": {"straddle": collect_block(sd, co.HEDGES, today, draws=draws, seed=seed),
                                  "put25": collect_block(pt, ("none",), today, draws=draws, seed=seed),
                                  "label": f"re-run at the {co.COLLECTABLE_TODAY} rates (STT rows unconfirmed)"}},
            "z": {"mf": st.z_stats([z for d, z in x.z_mf.items() if _in(d, rng)]),
                  "atm": st.z_stats([z for d, z in x.z_atm.items() if _in(d, rng)])},
        }  # fmt: skip
    return out
