"""The registration record of the seen-period risk-budget study (P4.2 Stage 2, #395): the hash of riskbudget/specs.py
and the sha256 of evals/experiments/risk_budget_seen/PREREG.md, both committed (and pushed) before any code that
replays a stored session existed. `require_registered` refuses the study while either differs."""

from __future__ import annotations

import hashlib
from pathlib import Path

from finresearch.riskbudget import specs

HASH = "801068ede3f169e03409550200847c6137125cbd3f8071aed1de9a7741459153"
PREREG_SHA256 = "27b6322ce959835d8d86d4917b21bed737d5b73294b15065737e08e9b7d98cac"
PREREG_FILE = "evals/experiments/risk_budget_seen/PREREG.md"
REGISTERED_ON = "2026-10-26"
NOTE = ("Registered before any code that replays a stored session existed: no fno_bhav, fno_contract, fno_iv_eod or "
        "span_param row was read for it, in the live database or any copy, and the 2008-2021 vault was not read.")  # fmt: skip


class PreregistrationError(RuntimeError):
    pass


def require_registered(root: Path | None = None) -> str:
    """The registered hash; PreregistrationError when specs.py, a setting it reads, or PREREG.md changed."""
    from finresearch.config import REPO_ROOT

    h = specs.registration_hash()
    if h != HASH:
        raise PreregistrationError(
            f"riskbudget/specs.py hashes to {h[:12]}… but the registered hash is {HASH[:12]}…: the study is refused. "
            "Record the change in evals/experiments/risk_budget_seen/ADDENDUM.md as a new SPEC_VERSION and commit "
            "its hash first."
        )
    f = (root or REPO_ROOT) / PREREG_FILE
    if not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest() != PREREG_SHA256:
        raise PreregistrationError(
            f"{PREREG_FILE} is missing or changed since registration: the study is refused (never edit PREREG.md; "
            "changes go to ADDENDUM.md)"
        )
    return h
