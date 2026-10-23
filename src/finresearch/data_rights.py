"""The owner's dated data-rights decision per collected dataset (#362, P1.0): a backfill or nightly job asks
`require(dataset)` before its first request and refuses to run without a recorded decision.

NSE's terms (Wayback memento of 01-Oct-2026, 'Updated on: 29/10/2025') prohibit "any systematic or automated data
collection activities" and use "for any gaming, virtual trading or simulation activities"; there is no personal-use
carve-out. The owner's decision of 21-Oct-2026 (issue #362) is to accept that risk for personal, non-distributed use
only, with these safeguards: the data stays in the local database and the gitignored data/ folder (never committed,
published or shared); polite access (the shared per-host rate limit, one archive file per dataset per day, backfills
spread out and run outside market hours); stop a dataset on repeated 403/429 or any notice and revisit the decision;
re-check NSE's terms before each new dataset.

The owner's decisions of 23-Oct-2026 (issue #471) extend accept-personal, under the same condition ("low risk only"),
to NSE's combined-OI and ban files, NSE's Nifty 50 member list and the members' quotes read for attribution, FBIL's
rates, IIMA's factor library and MoSPI's CPI. The same rules apply to FBIL, IIMA and MoSPI as to NSE: personal,
non-distributed use only (local database and data/, never committed, published or shared); polite access
(PoliteClient's per-host rate limit, disk caches so a source is read rarely); stop a source on repeated 403/429 or
any notice and revisit its decision; re-read a source's terms before a new dataset from it. FBIL's disclaimer ("FBIL
does not authorize commercial use of the data available in its website without written permission") is respected: no
commercial use and no redistribution. IIMA asks to be cited: the citation stays wherever its factors are shown.

Risk-free rates (#479, the owner's decision of 23-Oct-2026): no automated collection of rates from anyone. FBIL stays
paused (PENDING below) and RBI's data is not collected ("rbi_rates" is dropped): the app's risk-free rate is RBI's
policy repo rate typed in by hand from RBI's releases (fincalc.policy_rates) plus rates the owner enters.
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
_OWNER_1023, _REF_1023 = "2026-10-23", "issue #471"
# FBIL's T-bill, MIBOR and G-sec par curve history (#366): read 22-Oct-2026 from the disclaimer FBIL's benchmark pages
# show (www.fbil.org.in, site bundle main.12b820d6bf685718ad3d.js); no separate terms-of-use page was found.
_FBIL_TERMS = (
    "FBIL disclaimer read 22-Oct-2026: the benchmark is provided 'as is' without warranties; 'FBIL does not "
    "authorize commercial use of the data available in its website without written permission'; personal "
    "use not addressed. Respected: no commercial use, no redistribution"
)
# IIMA's Indian Fama-French-Momentum data library (#348, adapters.iima_factors): the page and its FAQ, read 22-Oct-2026
_IIMA_TERMS = (
    "IIMA Indian Fama-French-Momentum data library, page and FAQ read 22-Oct-2026: no licence, terms of use "
    "or redistribution permission stated, only a citation request (Agarwalla, Jacob and Varma 2013); the "
    "citation is kept wherever the factors are shown"
)
# MoSPI's CPI API (#347, adapters.mospi_cpi): CPI API User Manual.pdf (10-Apr-2024), read 22-Oct-2026
# RBI's website disclaimer (www.rbi.org.in/Scripts/Disclaimer.aspx, viewed 23-Oct-2026)
_RBI_TERMS = (
    "RBI website disclaimer, viewed 23-Oct-2026: 'Except as set forth below, caching and links to, and the framing "
    "of this Web Site or any of the contents are prohibited.' No RBI page or series is collected; policy-rate "
    "decisions are typed in by hand with their release (fincalc.policy_rates)"
)
_MOSPI_TERMS = (
    "MoSPI CPI API (government statistics), read 22-Oct-2026: the 2024 user manual describes a sign-up and "
    "a 15-minute access token ('Without access token the APIs will fetch only the first 10 records'); the "
    "live API serves pages without one; no licence text could be read; cited wherever shown"
)
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
        # The owner's decisions of 23-Oct-2026 (#471): accept-personal, the safeguards in the module docstring.
        # fbil_rates was accepted on 23-Oct-2026 on _FBIL_TERMS, then paused the same day (#476): FBIL's Terms of Use
        # of 13-Oct-2026 forbid it (see PENDING).
        Decision("nse_combineoi", "accept-personal", _OWNER_1023, _REF_1023,
                 f"{_NSE_TERMS}: automated collection prohibited (not re-read for it); ~1,200 daily files 2022-2026"),  # #374, nse_mwpl
        Decision("nse_secban", "accept-personal", _OWNER_1023, _REF_1023,
                 f"{_NSE_TERMS}: automated collection prohibited (not re-read for it); 20 dated files to check the ban history"),  # #374
        Decision("nse_index_members", "accept-personal", _OWNER_1023, _REF_1023,
                 f"{_NSE_TERMS}: the Nifty 50 member list and the members' quotes, for attribution; nothing stored"),
        Decision("iima_factors", "accept-personal", _OWNER_1023, _REF_1023, _IIMA_TERMS),  # #348/#430
        Decision("mospi_cpi", "accept-personal", _OWNER_1023, _REF_1023, _MOSPI_TERMS),  # #347/#436
        # #479: no RBI fetcher may run (T-bill auction results, money-market rates, DBIE): the owner dropped it
        Decision("rbi_rates", "drop", _OWNER_1023, "issue #479", _RBI_TERMS),
    )
}  # fmt: skip


# Datasets whose terms were read but whose decision is the owner's and not yet recorded: `require` refuses them and
# says what was read. To decide, add a Decision above (choice, date, where it is recorded, the terms it rests on).
PENDING: dict[str, str] = {
    # FBIL (#366/#447, adapters.fbil, refseries.rates and its nightly job; since #479 no rate lookup, archive or
    # signal reads FBIL, and FBIL's embedded curve is gone from the repository). FBIL's
    # "Terms of Use and Subscription Disclaimer for FBIL Benchmarks" (posted 13-Oct-2026, read 23-Oct-2026):
    # §9.1 the website shows the data after 7 calendar days "solely for delayed, non-commercial viewing purposes";
    # §9.2 users (including "casual website visitors") are "strictly prohibited from compiling, downloading, storing,
    # or leveraging the delayed website Benchmark Data to construct an internal database, historical archive, or
    # systematic tracking feed without acquiring a specific paid subscription"; §2.1 no redistribution or publishing.
    # The 23-Oct-2026 decision rested on the older disclaimer, so it is paused until the owner decides (#476); the
    # owner then chose the RBI repo-rate proxy without collecting anything (#479). A subscription would re-enable it.
    "fbil_rates": "FBIL Terms of Use, 13-Oct-2026: free website data is delayed 7 days and for non-commercial viewing "
    "only; building a database, archive or tracking feed from it needs a paid subscription (§9.2); no "
    "redistribution (§2.1). Paused pending the owner's decision (#476)",
    # NSE's daily FII derivative statistics (content/fo/fii_stats_<DD-Mon-YYYY>.xls, P1.9, #371,
    # adapters.nse_fii_stats): one ~10-20 KB workbook a day, ~2,900 requests for 2015-2026. Same NSE website terms as
    # above (not re-read for this dataset): no systematic or automated collection. Recorded as pending by the agent
    # that built the collector; the decision is the owner's.
    "fii_stats": f"{_NSE_TERMS}: automated collection prohibited; a new dataset (#371, one .xls a day, ~2,900 "
    "files 2015-2026). Owner decision needed (#371)",
    # NSE's per-symbol corporate actions (/api/corporates-corporateActions with from_date/to_date) for the stored
    # factor history (#369 slice 2, refseries.corp_actions): one request per F&O stock underlying, ~300 symbols once,
    # then only new symbols. The monitor's per-symbol fetch for watched stocks predates #362 and is not a backfill.
    # Same NSE website terms as above (not re-read for this dataset): no systematic or automated collection.
    "nse_corp_actions": f"{_NSE_TERMS}: automated collection prohibited; a new backfill (#369, ~300 per-symbol "
    "requests once). Owner decision needed (#369)",
    # NSE's symbol-change file (content/equities/symbolchange.csv on nsearchives [U: path as recalled]): one file, to
    # join old and new F&O symbols (#369). Same terms as above.
    "nse_symbol_change": f"{_NSE_TERMS}: automated collection prohibited; one file (#369). Owner decision needed "
    "(#369)",
    # BSE's corporate actions (adapters.bse_equity) as a factor source: BSE's terms of use were not read for any
    # decision in this repo. The terms parser reads BSE purposes offline; no BSE backfill exists (#369).
    "bse_corp_actions": "BSE terms of use not read: no BSE corporate-action backfill until the owner reads and decides "
    "(#369)",
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
