"""The registration record of skillmon/specs.py (#378): its hash, committed before the monitor computed any statistic
on a real outcome. `monitor.require_registered` refuses a report when specs.registration_hash() differs from HASH."""

HASH = "48a6ef33ede244488a3a489ed9ddc781b2443a92dc24c113756aeac255ea3de8"
REGISTERED_ON = "2026-10-22"
NOTE = ("Registered before any skill statistic was computed on a real outcome: no forecast-ledger row was read for "
        "it. The CUSUM thresholds come from synthetic simulation only (specs.CALIBRATION).")  # fmt: skip
