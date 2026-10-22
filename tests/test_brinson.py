"""Brinson-Fachler attribution (#348, fincalc.brinson): every expected value below is computed by hand in the comments,
and the identity allocation + selection + interaction = active return is checked on each example."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from finresearch.fincalc import brinson as b


def seg(name, wp, wb, rp, rb):
    f = lambda x: None if x is None else D(str(x))  # noqa: E731
    return b.Segment(name, f(wp), f(wb), f(rp), f(rb))


def test_three_segments_by_hand():
    # Rb = .4×.08 + .4×.04 + .2×(-.03) = .042;  Rp = .5×.10 + .3×.02 + .2×(-.05) = .046;  active = .004
    # A: alloc (.5-.4)(.08-.042) = .0038, sel .4(.10-.08) = .008, inter .1×.02 = .002  -> .0138
    # B: alloc (-.1)(.04-.042) = .0002, sel .4(.02-.04) = -.008, inter (-.1)(-.02) = .002 -> -.0058
    # C: alloc 0, sel .2(-.05+.03) = -.004, inter 0                                       -> -.004
    r = b.attribute(
        [seg("A", 0.5, 0.4, 0.10, 0.08), seg("B", 0.3, 0.4, 0.02, 0.04), seg("C", 0.2, 0.2, -0.05, -0.03)]
    )
    assert (r.portfolio_return, r.benchmark_return, r.active) == (D("0.046"), D("0.042"), D("0.004"))
    got = {x.name: (x.allocation, x.selection, x.interaction, x.total) for x in r.lines}
    assert got == {"A": (D("0.0038"), D("0.008"), D("0.002"), D("0.0138")),
                   "B": (D("0.0002"), D("-0.008"), D("0.002"), D("-0.0058")),
                   "C": (D("0"), D("-0.004"), D("0"), D("-0.004"))}  # fmt: skip
    assert (r.allocation, r.selection, r.interaction) == (D("0.0040"), D("-0.004"), D("0.004"))
    assert r.allocation + r.selection + r.interaction == r.active
    assert r.complete and r.reason is None


def test_off_benchmark_and_unheld_segments_by_hand():
    # X held and in the benchmark; Y in the benchmark only; Z (funds) held only.
    # Rb = .7×.05 + .3×.15 = .08;  Rp = .5×.10 + .5×.02 = .06;  active = -.02
    # X: alloc (-.2)(.05-.08) = .006, sel .7×.05 = .035, inter (-.2)(.05) = -.01 -> .031
    # Y (w_p = 0, r_p := r_b): alloc (-.3)(.15-.08) = -.021, sel 0, inter 0     -> -.021
    # Z (w_b = 0, r_b := r_p): alloc .5(.02-.08) = -.03, sel 0, inter 0          -> -.03
    r = b.attribute(
        [seg("X", 0.5, 0.7, 0.10, 0.05), seg("Y", 0, 0.3, None, 0.15), seg("Z", 0.5, 0, 0.02, None)]
    )
    assert r.active == D("-0.02")
    got = {x.name: (x.allocation, x.selection, x.interaction) for x in r.lines}
    assert got == {"X": (D("0.006"), D("0.035"), D("-0.01")), "Y": (D("-0.021"), D("0"), D("0")),
                   "Z": (D("-0.03"), D("0"), D("0"))}  # fmt: skip
    assert {x.name: x.note for x in r.lines}["Z"] == b.OFF_BENCHMARK
    assert {x.name: x.note for x in r.lines}["Y"] == b.NOT_HELD
    assert r.allocation + r.selection + r.interaction == r.active


def test_unknown_portfolio_segment_return_is_unknown_and_totals_are_none():
    r = b.attribute(
        [seg("A", 0.5, 0.5, None, 0.08), seg("B", 0.5, 0.5, 0.02, 0.04)], reasons={"A": "no price for AAA"}
    )
    a, bb = r.lines
    assert (a.allocation, a.selection, a.interaction, a.total) == (None, None, None, None)
    assert a.reason == "no price for AAA"
    # B is still known: Rb = .06, alloc 0, sel .5(.02-.04) = -.01
    assert (bb.allocation, bb.selection, bb.interaction) == (D("0"), D("-0.01"), D("0"))
    assert r.portfolio_return is None and r.active is None and r.allocation is None and not r.complete
    assert r.benchmark_return == D("0.06")
    assert "A" in r.reason


def test_unknown_benchmark_segment_return_makes_every_line_unknown():
    r = b.attribute([seg("A", 0.5, 0.5, 0.10, None), seg("B", 0.5, 0.5, 0.02, 0.04)])
    assert r.benchmark_return is None and r.active is None and not r.complete
    assert all(x.allocation is None and x.reason for x in r.lines)  # allocation needs the benchmark's total
    assert r.lines[0].reason == "benchmark return of A unknown"


def test_weights_must_sum_to_one():
    with pytest.raises(ValueError, match=r"portfolio weights sum to 0\.9"):
        b.attribute([seg("A", 0.5, 0.5, 0.1, 0.1), seg("B", 0.4, 0.5, 0.1, 0.1)])
