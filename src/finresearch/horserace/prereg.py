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

# Post-hoc addendum A6 (horserace/addendum_a6.py, ADDENDUM.md): the exit-rule Monte Carlo challenger p_path (#388),
# with its simulation settings (paths, seed, steps) and path-model constants, pinned before any replay code computed
# it on a stored session.
ADDENDUM_A6_HASH = "18f7b40274feef48aca3760e8244a37d1c650118fc9dcd1c8b2c115ba244881b"
ADDENDUM_A6_ON = "2026-10-22"
