"""The registration record of horserace/specs.py (#377): its hash, committed before any score was computed on real
outcomes. `race.require_registered` refuses a run when specs.registration_hash() differs from HASH."""

HASH = "11f10c09f6a629ec09b3115a37008afaf0f2917b69cb56abd716157be4701060"
REGISTERED_ON = "2026-10-22"
NOTE = ("Registered before any horse-race score was computed: no card-ledger outcome and no replayed outcome had "
        "been read, and no stored F&O row was queried for it (the counts in PREREG.md are issue #377's).")  # fmt: skip

# Post-hoc addendum A1 (horserace/addendum.py, evals/experiments/card_horse_race/ADDENDUM.md): the replay's open
# choices, pinned before any replay code read a stored session; replay.require_registered checks both hashes.
ADDENDUM_HASH = "d26e9f0d58a8f86cb66433f59053633a98efdab2f824c7ee7112f76c4e3bd3a6"
ADDENDUM_ON = "2026-10-22"

# Post-hoc addendum A2 (horserace/addendum_a2.py, ADDENDUM.md): the business-time clock challenger p_rw_bus (#384),
# pinned before any replay code computed it on a stored session.
ADDENDUM_A2_HASH = "a43dd5c2bd94d48e304e84ca7da391d7841f810de8e261eb12c12addf37022c0"
ADDENDUM_A2_ON = "2026-10-22"

# Post-hoc addendum A3 (horserace/addendum_a3.py, ADDENDUM.md): the forward/IV challenger p_rn_b76 (#385), with the
# Black-76 engine's registered constants, pinned before any replay code computed it on a stored session.
ADDENDUM_A3_HASH = "c5c07f159a6a8154ac7f264a847645fd4556b69c86fd1e1808bf8bd9b64dc10b"
ADDENDUM_A3_ON = "2026-10-22"

# Post-hoc addendum A4 (horserace/addendum_a4.py, ADDENDUM.md): the SVI risk-neutral density challenger p_rn_svi
# (#386), with the fit's and density's registered settings, pinned before any replay code computed it on a stored
# session.
ADDENDUM_A4_HASH = "3602512dc19256374975c5f8779a7890ea16eb15d5471871be056a6684cbd6da"
ADDENDUM_A4_ON = "2026-10-22"

# Post-hoc addendum A5 (horserace/addendum_a5.py, ADDENDUM.md): the tail-aware physical challengers p_rw_emp (IV-scaled
# empirical) and p_rw_fhs (GJR filtered historical simulation) of P3.4 (#387), with every fitted setting of
# fincalc/taildist.py, pinned before any replay code computed them on a stored session; chained on A4 (re-pinned when A4 merged first,
# still before any replay run).
ADDENDUM_A5_HASH = "cebb9fd453637c294ee9cac2c1eeb545daa9a033523aca2236be7e66a834a670"
ADDENDUM_A5_ON = "2026-10-22"

# Post-hoc addendum A6 (horserace/addendum_a6.py, ADDENDUM.md): the exit-rule Monte Carlo challenger p_path (#388),
# with its simulation settings (paths, seed, steps) and path-model constants, pinned before any replay code computed
# it on a stored session.
# Re-chained before any replay run: first on A1-A3 (7dfe96c6...89dca7f3), then on A4 (18f7b402...ba244881b), now on A5.
ADDENDUM_A6_HASH = "5c34c776b3fa171863eec95b71ae73a5d6f2de1eca38a0a7d7cbfd8ef38bdc14"
ADDENDUM_A6_ON = "2026-10-22"
