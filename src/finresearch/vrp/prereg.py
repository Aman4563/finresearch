"""The registration record of vrp/specs.py (P3.6, #389): its hash, committed before any code that reads a stored
session existed. `require_registered` refuses a run while specs.registration_hash() differs from HASH."""

from finresearch.vrp import specs

HASH = "73b3e015921e2b636c32b5d5b54f92a7baa53c390d2a26d15fe8c4cba1737aad"
REGISTERED_ON = "2026-10-22"
NOTE = ("Registered before any study code existed that could read a stored session: no fno_bhav, fno_contract or "
        "index_eod row was queried for it, and the 2008-2021 vault was not read.")  # fmt: skip


class PreregistrationError(RuntimeError):
    pass


def require_registered() -> str:
    h = specs.registration_hash()
    if h != HASH:
        raise PreregistrationError(
            f"vrp/specs.py hashes to {h[:12]}… but the registered hash is {HASH[:12]}…: the study is refused. "
            "Record the change in evals/experiments/vrp_collectable/ADDENDUM.md as a new SPEC_VERSION and commit "
            "its hash first."
        )
    return h
