"""Feature drift of the direction model's and the F&O card's inputs (#378, skill-monitor-v2, registered in
skillmon/specs_v2.py): PSI of each input's live sample against its reference window, on the reference's decile bins.

Per input: the reference sample is every known value inside the model's reference window (at least
DRIFT.reference_min), the live sample its last DRIFT.live_values known values after the window. Too few known values on
either side read "insufficient data" with the reason, never ok; PSI >= DRIFT.watch is watch and >= DRIFT.act is act
(an alarm). Pure functions here; nothing reads the database."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from finresearch.skillmon import specs_v2
from finresearch.skillmon import stats as st


def _known(xs: Sequence[float | None]) -> list[float]:
    return [float(x) for x in xs if x is not None and math.isfinite(float(x))]


def status_of(psi: float) -> str:
    d = specs_v2.DRIFT
    return "act" if psi >= d.act else "watch" if psi >= d.watch else "ok"


def input_psi(reference: Sequence[float | None], live: Sequence[float | None]) -> dict[str, Any]:
    """PSI of one input: `reference` = its values inside the reference window, `live` = its values after it, oldest
    first (None = unknown, dropped)."""
    d = specs_v2.DRIFT
    ref, tail = _known(reference), _known(live)[-d.live_values :]
    out: dict[str, Any] = {"psi": None, "status": "insufficient data", "n_ref": len(ref), "n_live": len(tail),
                           "bins": None, "why": None}  # fmt: skip
    if len(ref) < d.reference_min:
        out["why"] = f"{len(ref)} known reference values of the {d.reference_min} needed"
        return out
    if len(tail) < d.live_values:
        out["why"] = f"{len(tail)} known live values of the {d.live_values} needed"
        return out
    edges = st.decile_edges(ref)
    psi = st.psi(st.bin_counts(ref, edges), st.bin_counts(tail, edges), eps=d.eps)
    out.update(
        psi=psi, status=status_of(psi) if psi is not None else "insufficient data", bins=len(edges) + 1
    )
    return out
