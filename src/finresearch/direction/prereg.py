"""The registration record of direction/specs.py (#327): its hash, committed before any evaluation on real data.

`evaluate.require_registered` compares specs.registration_hash() with HASH and refuses an out-of-sample run when they
differ, so a spec changed after registration can never be evaluated out of sample as if it were the original.
"""

HASH = "372dabfe1d070d22f6a1e45c8994c6b3fc5dbefbba06bed19d4f0a6296adae4d"
REGISTERED_ON = "2026-10-19"
NOTE = ("Registered before any feature, forecast or metric was computed on real data: the market-data copy "
        "(finresearch_fnohist2) had been inspected only for its tables, row counts and date coverage.")  # fmt: skip
