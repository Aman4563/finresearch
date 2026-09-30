"""Household finances (/wealth): net worth, balance-sheet allocation, goals, emergency fund, insurance and debt.

Deterministic Python only. Personal data stays in the local database and is never sent to an LLM. Outputs are
arithmetic on the user's own entries and stated assumptions: "your target says", "the numbers imply", "by the
needs method" - never an instruction. Rules of thumb are labelled as such; facts that could not be checked against
an official source are tagged [unverified] here and in the UI.
"""

from __future__ import annotations

from finresearch.signals.base import DISCLAIMER as _SIGNAL_DISCLAIMER

PRIVACY = "Your own entries, kept in the local database only and never sent to an LLM."
DISCLAIMER = (
    f"{_SIGNAL_DISCLAIMER} Household figures are arithmetic on your entries and stated assumptions; rules of thumb "
    "are labelled; insurance figures are generic arithmetic, not a product recommendation."
)
