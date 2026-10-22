"""The registration record of volrace/specs.py (P3.7, #390): its hash, committed before any race code read a stored
session. `require_registered` refuses a run while specs.registration_hash() differs from HASH."""

from finresearch.volrace import specs

HASH = "5f04e07945d85ffcae2bb2df2fc122af648d585e6e3a8724e1204d9889eed45c"
REGISTERED_ON = "2026-10-22"
NOTE = ("Registered before any race code existed that could read a stored session: no fno_bhav or fno_iv_eod row was "
        "queried for it, and the 2008-2021 vault is not loaded anywhere this project controls.")  # fmt: skip


class PreregistrationError(RuntimeError):
    pass


def require_registered() -> str:
    h = specs.registration_hash()
    if h != HASH:
        raise PreregistrationError(
            f"volrace/specs.py hashes to {h[:12]}… but the registered hash is {HASH[:12]}…: the race is refused. "
            "Record the change in evals/experiments/vol_forecast_race/ADDENDUM.md as a new SPEC_VERSION and commit "
            "its hash first."
        )
    return h
