"""The registration record of horserace/specs.py (#377): its hash, committed before any score was computed on real
outcomes. `race.require_registered` refuses a run when specs.registration_hash() differs from HASH."""

HASH = "11f10c09f6a629ec09b3115a37008afaf0f2917b69cb56abd716157be4701060"
REGISTERED_ON = "2026-10-22"
NOTE = ("Registered before any horse-race score was computed: no card-ledger outcome and no replayed outcome had "
        "been read, and no stored F&O row was queried for it (the counts in PREREG.md are issue #377's).")  # fmt: skip
