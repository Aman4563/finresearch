"""The registration record of the stock-option VRP study (P1.12, #374): the hash of optpanel/specs.py and the sha256
of evals/experiments/stock_option_vrp/PREREG.md, both committed before any panel code existed. `require_registered`
refuses every panel or ban-history load while either differs, so the panel is built only behind the registered
study."""

from __future__ import annotations

import hashlib
from pathlib import Path

from finresearch.optpanel import specs

HASH = "8a657266082489e519659bef4d3a7817462e6bf8a40b2340860cdbc6023093e2"
PREREG_SHA256 = "61393b5f6ace344120e6311a6ad055a4a8af40f35bc7d8dab34b327aee6add17"
PREREG_FILE = "evals/experiments/stock_option_vrp/PREREG.md"
REGISTERED_ON = "2026-10-22"
NOTE = ("Registered before any panel code existed: no stock-option row outside the held or watched stocks has been "
        "parsed into any table, and no combineoi or dated ban file has been downloaded.")  # fmt: skip


class PreregistrationError(RuntimeError):
    pass


def require_registered(root: Path | None = None) -> str:
    """The registered hash; PreregistrationError when specs.py or PREREG.md changed since registration."""
    from finresearch.config import REPO_ROOT

    h = specs.registration_hash()
    if h != HASH:
        raise PreregistrationError(
            f"optpanel/specs.py hashes to {h[:12]}… but the registered hash is {HASH[:12]}…: the panel is refused. "
            "Record the change in evals/experiments/stock_option_vrp/ADDENDUM.md as a new SPEC_VERSION and commit "
            "its hash first."
        )
    f = (root or REPO_ROOT) / PREREG_FILE
    if not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest() != PREREG_SHA256:
        raise PreregistrationError(
            f"{PREREG_FILE} is missing or changed since registration: the panel is refused (never edit PREREG.md; "
            "changes go to ADDENDUM.md)"
        )
    return h
