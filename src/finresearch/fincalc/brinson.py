"""Brinson-Fachler single-period attribution (#348).

Brinson, G. and Fachler, N. (1985), "Measuring non-US equity portfolio performance", Journal of Portfolio Management
11(3); the segment formulas as in Bacon, "Practical Portfolio Performance Measurement and Attribution" (2nd ed.,
2008), ch. 5. For each segment s (a sector or an asset class), with start-of-period weights w_p, w_b, segment returns
r_p, r_b, and the benchmark's total R_b = Σ w_b·r_b:

    allocation   A_s = (w_p − w_b)·(r_b − R_b)
    selection    S_s = w_b·(r_p − r_b)
    interaction  I_s = (w_p − w_b)·(r_p − r_b)

Σ (A + S + I) = R_p − R_b exactly, because Σ (w_p − w_b)·R_b = 0 when both weight sets sum to one.

Conventions for a segment one side does not hold (stated in METHOD and returned with each line):
- held but not in the benchmark (w_b = 0): r_b is undefined and taken as r_p, so the whole effect is allocation,
  w_p·(r_p − R_b): the choice to be outside the benchmark;
- in the benchmark but not held (w_p = 0): r_p is taken as r_b, so the effect is allocation, −w_b·(r_b − R_b).

Unknown stays unknown: a held segment without a return, or a benchmark segment without one, makes that line None with a
reason; an unknown benchmark segment also makes R_b and so every allocation unknown; any unknown line makes the totals
None (the known lines are never summed into a "total"). Decimal throughout; weights must each sum to one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

OFF_BENCHMARK = (
    "not in the benchmark: its benchmark return is taken as your return, so the effect is all allocation"
)
NOT_HELD = "not held: your return is taken as the benchmark's, so the effect is all allocation"
METHOD = ("Brinson-Fachler (1985): allocation (w_p − w_b)(r_b − R_b), selection w_b(r_p − r_b), interaction "
          "(w_p − w_b)(r_p − r_b); they add up to the active return R_p − R_b. A segment outside the benchmark counts "
          "as allocation against the benchmark's total; a benchmark segment you do not hold likewise.")  # fmt: skip
WEIGHT_TOL = Decimal("0.000001")


@dataclass(frozen=True)
class Segment:
    name: str
    wp: Decimal | None  # portfolio weight at the start (None: unknown)
    wb: Decimal | None  # benchmark weight at the start
    rp: Decimal | None  # portfolio segment return over the period (ignored when wp == 0)
    rb: Decimal | None  # benchmark segment return (ignored when wb == 0)


@dataclass(frozen=True)
class Line:
    name: str
    wp: Decimal | None
    wb: Decimal | None
    rp: Decimal | None
    rb: Decimal | None
    allocation: Decimal | None = None
    selection: Decimal | None = None
    interaction: Decimal | None = None
    note: str | None = None
    reason: str | None = None

    @property
    def total(self) -> Decimal | None:
        if self.allocation is None or self.selection is None or self.interaction is None:
            return None
        return self.allocation + self.selection + self.interaction


@dataclass(frozen=True)
class Result:
    lines: list[Line]
    portfolio_return: Decimal | None
    benchmark_return: Decimal | None
    active: Decimal | None
    allocation: Decimal | None
    selection: Decimal | None
    interaction: Decimal | None
    reason: str | None = None
    unknown: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.reason is None


def _sum(xs: Sequence[Decimal | None]) -> Decimal | None:
    return None if any(x is None for x in xs) else sum(xs, Decimal(0))  # type: ignore[arg-type]


def attribute(segments: Sequence[Segment], reasons: Mapping[str, str] | None = None) -> Result:
    """The attribution of `segments`; `reasons` explains why a segment's inputs are unknown."""
    reasons = reasons or {}
    for side, ws in (("portfolio", [s.wp for s in segments]), ("benchmark", [s.wb for s in segments])):
        tot = _sum(ws)
        if tot is not None and abs(tot - 1) > WEIGHT_TOL:
            raise ValueError(f"{side} weights sum to {tot.normalize()}, not 1")

    def why(s: Segment, default: str) -> str:
        return reasons.get(s.name) or default

    # the benchmark side: R_b needs every benchmark segment's weight and return
    bad_b = [s for s in segments if s.wb is None or (s.wb != 0 and s.rb is None)]
    rb_tot = None if bad_b else sum((s.wb * s.rb for s in segments if s.wb), Decimal(0))  # type: ignore[operator]
    bad_p = [s for s in segments if s.wp is None or (s.wp != 0 and s.rp is None)]
    rp_tot = None if bad_p else sum((s.wp * s.rp for s in segments if s.wp), Decimal(0))  # type: ignore[operator]

    lines: list[Line] = []
    for s in segments:
        base = dict(name=s.name, wp=s.wp, wb=s.wb, rp=s.rp, rb=s.rb)
        if any(s is x for x in bad_b):
            lines.append(Line(**base, reason=why(s, f"benchmark return of {s.name} unknown")))
            continue
        if any(s is x for x in bad_p):
            lines.append(Line(**base, reason=why(s, f"portfolio return of {s.name} unknown")))
            continue
        if rb_tot is None:
            names = ", ".join(x.name for x in bad_b)
            lines.append(Line(**base, reason=f"the benchmark's total return is unknown ({names})"))
            continue
        wp, wb = s.wp, s.wb
        assert wp is not None and wb is not None
        note = None
        if wb == 0 and wp == 0:
            rp = rb = Decimal(0)
        elif wb == 0:
            rp = rb = s.rp  # type: ignore[assignment]
            note = OFF_BENCHMARK
        elif wp == 0:
            rp = rb = s.rb  # type: ignore[assignment]
            note = NOT_HELD
        else:
            rp, rb = s.rp, s.rb  # type: ignore[assignment]
        a = (wp - wb) * (rb - rb_tot)
        lines.append(
            Line(**base, allocation=a, selection=wb * (rp - rb), interaction=(wp - wb) * (rp - rb), note=note)
        )

    unknown = [x.name for x in lines if x.total is None]
    reason = f"unknown: {', '.join(unknown)}" if unknown else None
    tot = lambda f: None if unknown else sum((getattr(x, f) for x in lines), Decimal(0))  # noqa: E731
    active = None if unknown or rp_tot is None or rb_tot is None else rp_tot - rb_tot
    return Result(lines, rp_tot, rb_tot, active, tot("allocation"), tot("selection"), tot("interaction"),
                  reason=reason, unknown=unknown)  # fmt: skip
