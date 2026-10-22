"""The registration record of fincalc/beta_specs.py (#394, #395): its hash, committed before the estimator read any
stored futures price. `fincalc.beta.require_registered` refuses an estimate when beta_specs.registration_hash()
differs from HASH."""

HASH = "13c22ee1e527d32529eaa14514e6d0a72c29a40fc84ee914857cd860d141b892"
REGISTERED_ON = "2026-10-22"
NOTE = ("Registered before the estimator read any stored futures price: no fno_bhav row of the live database was "
        "read for it; its tests use synthetic series only.")  # fmt: skip
