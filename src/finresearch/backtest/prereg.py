"""The registration record of specs.py (#307): its hash, committed before any out-of-sample evaluation on real data.

`evaluate.require_registered` compares specs.registration_hash() with HASH and refuses an out-of-sample run when
they differ, so a spec changed after registration can never be evaluated out of sample as if it were the original.
"""

HASH = "pending"
REGISTERED_ON = "2026-10-12"
NOTE = ("Registered before the market-data copy of the F&O history (finresearch_fnohist) was available to this "
        "branch: no in-sample or out-of-sample result on real data had been computed.")  # fmt: skip
