"""The studies registered in the vault pack (#364, evals/experiments/vault/PREREG.md) and how their hashes are computed.

A study's hash covers its registry entry (prerequisites, engine pin, repeatability), the bytes of every file that
registers it, the pack's access-rule file, and for the library studies the hashes of the 8 registered specs of
backtest/specs.py with the book and criteria (a spec added later, such as a conditional variant, leaves them as they are).
The pack hash covers every study hash, so adding or changing any study changes it. The recorded values live in
`vault/pack.py`; `vault.guard` recomputes them from disk before every read and refuses on any difference.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

# The vault: index options and futures with a trade day in this range (#364, #365). 2022 on is the stored history.
VAULT_START, VAULT_END = date(2008, 1, 1), date(2021, 12, 31)
RULES_FILE = "evals/experiments/vault/PREREG.md"
# The library v1 specs as registered on 12-Oct-2026 (backtest/prereg.py, hash 6d92407630d7…)
LIBRARY_KEYS = ("nifty_pcs_m", "nifty_ccs_m", "banknifty_ic_m", "nifty_ic_w", "nifty_dcs_m", "nifty_dps_m",
                "stock_cc_m", "stock_pp_m")  # fmt: skip


class VaultSealed(PermissionError):
    """A read or load of vault-period data was refused (fail closed)."""


@dataclass(frozen=True)
class Study:
    id: str
    roadmap: str
    files: tuple[str, ...]  # repo-relative files that register it
    requires: tuple[str, ...] = ()  # studies that must have a logged read first
    engine_pin: bool = False  # reads only with the pinned backtest engine (PREREG.md §2.7)
    repeatable: bool = False  # only the aggregate data audit
    counts_in_n: bool = True  # counted in the trial count N (vault.guard.reads_count)
    library: bool = False  # replays the 8 registered specs (backtest/specs.py), whose hashes it covers


_V = "evals/experiments/vault/"
_AUDIT = "vault_data_audit"
STUDIES: dict[str, Study] = {
    s.id: s
    for s in (
        Study(_AUDIT, "P1.3", (_V + "audit.md",), repeatable=True, counts_in_n=False),
        Study("library_v1_same_print", "P1.2", (_V + "library_v1.md",), (_AUDIT,), engine_pin=True, library=True),
        Study("library_v1_next_print", "P2.7a", (_V + "library_v1.md",), (_AUDIT,), engine_pin=True, library=True),
        Study("horse_race_calibration", "P2.2+P3.4", ("evals/experiments/fno_pop_calibration/PREREG.md",), (_AUDIT,)),
        Study("vrp_primary", "P3.6", (_V + "vrp.md",), (_AUDIT,)),
        Study("vol_forecast_race", "P3.7", (_V + "vol_race.md",), ("vrp_primary",)),
        Study("vrp_seller_variant", "P3.7", (_V + "vol_race.md", _V + "vrp.md"),
              ("library_v1_same_print", "vrp_primary", "vol_forecast_race"), engine_pin=True),
        Study("event_vol_fairness", "P3.8", (_V + "event_vol.md",), (_AUDIT,)),
        Study("event_skip_variant", "P3.8", (_V + "event_vol.md", _V + "vrp.md"),
              ("library_v1_same_print", "vrp_primary"), engine_pin=True),
        Study("risk_budget_counterfactual", "P4.2", (_V + "risk_budget.md",), ("library_v1_same_print",),
              engine_pin=True, library=True),
        Study("crisis_day_replays", "P4.1", (_V + "stress.md",), (_AUDIT,)),
        Study("stress_start_day_scan", "P4.1", (_V + "stress.md",), ("horse_race_calibration", "library_v1_same_print")),
        Study("risk_model_samples", "P3.4+P4.1", (_V + "stress.md",), ("horse_race_calibration",)),
    )
}  # fmt: skip


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def file_hash(rel: str, root: Path) -> str | None:
    """SHA-256 of a registered file's bytes; None when it is missing (the guard then refuses)."""
    p = root / rel
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def study_hash(study: Study, root: Path) -> str | None:
    files = {f: file_hash(f, root) for f in (*study.files, RULES_FILE)}
    if any(v is None for v in files.values()):
        return None
    body: dict[str, object] = {"study": asdict(study), "files": files}
    if study.library:
        from finresearch.backtest import specs

        try:
            body["library"] = {"specs": {k: specs.spec_hash(specs.by_key(k)) for k in LIBRARY_KEYS},
                               "book": asdict(specs.BOOK), "criteria": asdict(specs.CRITERIA)}  # fmt: skip
        except KeyError:
            return None
    return hashlib.sha256(canonical(body).encode()).hexdigest()


def pack_hash(root: Path) -> str | None:
    hashes = {k: study_hash(s, root) for k, s in STUDIES.items()}
    if any(v is None for v in hashes.values()):
        return None
    return hashlib.sha256(canonical(hashes).encode()).hexdigest()


def engine_hash(src: Path | None = None) -> str:
    """The engine pin's definition (PREREG.md §2.7): SHA-256 over the sorted relative paths and bytes of backtest/*.py,
    fincalc/options.py and fincalc/charges.py."""
    src = src or Path(__file__).resolve().parents[1]
    files = sorted(
        [*(src / "backtest").glob("*.py"), src / "fincalc" / "options.py", src / "fincalc" / "charges.py"]
    )
    h = hashlib.sha256()
    for f in files:
        h.update(f.relative_to(src).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()
