"""The owner's dated data-rights decision per collected dataset (#362, P1.0): a backfill or nightly job asks
`require(dataset)` before its first request and refuses to run without a recorded decision.

NSE's terms (Wayback memento of 01-Oct-2026, 'Updated on: 29/10/2025') prohibit "any systematic or automated data
collection activities" and use "for any gaming, virtual trading or simulation activities"; there is no personal-use
carve-out. The owner's decision of 21-Oct-2026 (issue #362) is to accept that risk for personal, non-distributed use
only, with these safeguards: the data stays in the local database and the gitignored data/ folder (never committed,
published or shared); polite access (the shared per-host rate limit, one archive file per dataset per day, backfills
spread out and run outside market hours); stop a dataset on repeated 403/429 or any notice and revisit the decision;
re-check NSE's terms before each new dataset.
"""

from __future__ import annotations

from dataclasses import dataclass


class NoDecision(RuntimeError):
    pass


@dataclass(frozen=True)
class Decision:
    dataset: str
    choice: str  # accept-personal | license | drop
    decided: str  # ISO date
    ref: str  # where the decision is recorded
    terms_read: str  # the terms version the decision rests on


_NSE_TERMS = "NSE website terms, Wayback memento of 01-Oct-2026 ('Updated on: 29/10/2025')"
DECISIONS: dict[str, Decision] = {
    d.dataset: d
    for d in (
        Decision(
            "index_eod", "accept-personal", "2026-10-21", "issue #362", _NSE_TERMS
        ),  # ind_close_all (#366)
        Decision(
            "nse_span", "accept-personal", "2026-10-21", "issue #362", _NSE_TERMS
        ),  # nsccl SPAN + FOVOLT (#370)
        Decision(
            "fno_participant_oi", "accept-personal", "2026-10-21", "issue #362", _NSE_TERMS
        ),  # nsccl fao_participant_oi (#317; backfill #371)
        Decision(
            "fno_participant_vol", "accept-personal", "2026-10-21", "issue #362", _NSE_TERMS
        ),  # nsccl fao_participant_vol (#371)
        Decision(
            "equity_eod", "accept-personal", "2026-10-21", "issue #362", _NSE_TERMS
        ),  # products/content/sec_bhavdata_full (#373 (a))
        Decision(
            "fno_bhav", "accept-personal", "2026-10-21", "issue #362", _NSE_TERMS
        ),  # the F&O bhavcopy (#306, collected since before #362) and its 2008-2021 vault load (#365)
    )
}


# Datasets whose terms were read but whose decision is the owner's and not yet recorded: `require` refuses them and
# says what was read. To decide, add a Decision above (choice, date, where it is recorded, the terms it rests on).
PENDING: dict[str, str] = {
    # FBIL's T-bill, MIBOR and G-sec par curve history (#366, refseries.rates). Read 22-Oct-2026 from the disclaimer
    # FBIL's benchmark pages show (www.fbil.org.in, site bundle main.12b820d6bf685718ad3d.js): the benchmark is
    # provided "as is" without warranties, and "FBIL does not authorize commercial use of the data available in its
    # website without written permission". No separate terms-of-use page was found; personal use is not addressed.
    "fbil_rates": "FBIL disclaimer read 22-Oct-2026: no commercial use without written permission; personal use "
    "not addressed. Owner decision needed (#366)",
    # NSE Clearing's combined OI / MWPL file (P1.12, #374, adapters.nse_mwpl): one zip a day on nsearchives, ~1,200
    # requests for 2022-2026. Same NSE website terms as the datasets above ('Updated on: 29/10/2025', read through
    # the 01-Oct-2026 Wayback memento for #362; not re-read for this dataset): no systematic or automated collection.
    "nse_combineoi": f"{_NSE_TERMS}: automated collection prohibited; a new dataset (#374, ~1,200 daily files "
    "2022-2026). Owner decision needed (#374)",
    # NSE's dated F&O ban notice (fo_secban_DDMMYYYY.csv, P1.12, #374): 20 files, only to validate the derived ban
    # history. Same terms as above.
    "nse_secban": f"{_NSE_TERMS}: automated collection prohibited; 20 dated files to validate the ban history "
    "(#374). Owner decision needed (#374)",
    # NSE's daily FII derivative statistics (content/fo/fii_stats_<DD-Mon-YYYY>.xls, P1.9, #371,
    # adapters.nse_fii_stats): one ~10-20 KB workbook a day, ~2,900 requests for 2015-2026. Same NSE website terms as
    # above (not re-read for this dataset): no systematic or automated collection. Recorded as pending by the agent
    # that built the collector; the decision is the owner's.
    "fii_stats": f"{_NSE_TERMS}: automated collection prohibited; a new dataset (#371, one .xls a day, ~2,900 "
    "files 2015-2026). Owner decision needed (#371)",
}


def require(dataset: str) -> Decision:
    """The dataset's decision; NoDecision when none is recorded or the owner chose to drop it."""
    d = DECISIONS.get(dataset)
    if d is None:
        why = f" ({PENDING[dataset]})" if dataset in PENDING else ""
        raise NoDecision(f"no recorded data-rights decision for {dataset!r} (#362){why}: not collecting it")
    if d.choice == "drop":
        raise NoDecision(f"the owner dropped {dataset!r} on {d.decided} ({d.ref})")
    return d
