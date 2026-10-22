"""The beta-to-NIFTY estimator for the F&O book (#394, #395): its registration and the pure estimator, against
values computed by hand in the comments (or by an independent formula)."""

from __future__ import annotations

from pathlib import Path

from finresearch.fincalc import beta_prereg, beta_specs


def test_the_registration_is_pinned_and_quoted_in_prereg_md() -> None:
    assert beta_specs.registration_hash() == beta_prereg.HASH
    text = (Path(__file__).resolve().parents[1] / "evals/experiments/book_betas/PREREG.md").read_text()
    s = beta_specs.SPEC
    assert beta_prereg.HASH in text and beta_specs.SPEC_VERSION in text
    for needle in (f"half-life of **{s.half_life_sessions} sessions**", f"**{s.window_sessions}**",
                   f"**{s.min_returns}**", f"z = {s.z}", f"floor of **{s.floor:g}**"):  # fmt: skip
        assert needle in text, needle
