"""The registration record of skillmon/specs.py (#378): its hash, committed before the monitor computed any statistic
on a real outcome. `monitor.require_registered` refuses a report when specs.registration_hash() differs from HASH."""

HASH = "48a6ef33ede244488a3a489ed9ddc781b2443a92dc24c113756aeac255ea3de8"
REGISTERED_ON = "2026-10-22"
NOTE = ("Registered before any skill statistic was computed on a real outcome: no forecast-ledger row was read for "
        "it. The CUSUM thresholds come from synthetic simulation only (specs.CALIBRATION).")  # fmt: skip

# skill-monitor-v2 (skillmon/specs_v2.py, evals/experiments/skill_monitoring/ADDENDUM.md, post hoc): feature drift,
# the IPO ranges' coverage with acceptance (d) and the Kupiec risk trigger, pinned before any code read a stored
# input, an outcome or an IPO row for it. `monitor.require_registered_v2` refuses v2 while the two differ.
V2_HASH = "0ab79edc34b1ef38f671b61dc12b24ba9087c454b26736dc2e3a9b15d6e6e2ee"
V2_REGISTERED_ON = "2026-10-26"
V2_NOTE = ("Registered before any v2 statistic was computed on real data: no stored F&O session, VaR-ledger row or "
           "ipo_history row was read for it, and the walk-forward artefact's coverage figures were not opened. "
           "Its known limits come from synthetic simulation and exact binomial sums only.")  # fmt: skip
