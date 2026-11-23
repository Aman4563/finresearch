"""The registration record of the stock factor diagnostics (#506): the hash of factordiag/specs.py and the sha256 of
evals/experiments/stock_factor_diagnostics/PREREG.md, both committed (and pushed) before any code that could read a
price existed. `require_registered` refuses the study while either differs, or while one of the two pinned membership
files of the base experiment differs."""

from __future__ import annotations

import hashlib
from pathlib import Path

from finresearch.factordiag import specs

HASH = "3a536c80d8252444ef2540d23d66f5c5f6a51549c51c7728a5c85575d7a73664"
PREREG_SHA256 = "0b20aa7c3a9c5a9d91de9640ba46af21c93ae9b50eee72138a42f457dd6e5a28"
PREREG_FILE = "evals/experiments/stock_factor_diagnostics/PREREG.md"
REGISTERED_ON = "2026-11-23"
NOTE = ("Registered before any code that could read a price existed: no file of the price cache, no cached market "
        "data and no database row was read for it. Its calibration comes from generated arrays only.")  # fmt: skip


class PreregistrationError(RuntimeError):
    pass


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def require_registered(root: Path | None = None) -> str:
    """The registered hash; PreregistrationError when specs.py, a setting it reads, PREREG.md or a pinned membership
    file changed."""
    from finresearch.config import REPO_ROOT

    h = specs.registration_hash()
    if h != HASH:
        raise PreregistrationError(
            f"factordiag/specs.py hashes to {h[:12]}… but the registered hash is {HASH[:12]}…: the diagnostic is "
            "refused. Record the change in evals/experiments/stock_factor_diagnostics/ADDENDUM.md as a new "
            "SPEC_VERSION and commit its hash first."
        )
    base = root or REPO_ROOT
    if _sha256(base / PREREG_FILE) != PREREG_SHA256:
        raise PreregistrationError(
            f"{PREREG_FILE} is missing or changed since registration: the diagnostic is refused (never edit "
            "PREREG.md; changes go to ADDENDUM.md)"
        )
    for name, want in ((specs.CHANGES_FILE, specs.CHANGES_SHA256),
                       (specs.CONSTITUENTS_FILE, specs.CONSTITUENTS_SHA256)):  # fmt: skip
        if _sha256(base / name) != want:
            raise PreregistrationError(
                f"{name} is missing or differs from the membership file pinned at registration: the diagnostic is "
                "refused (a corrected membership log is a new SPEC_VERSION)"
            )
    return h
