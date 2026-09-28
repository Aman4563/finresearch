"""Deterministic financial arithmetic for research reports ("Python computes and verifies").

Library-wide conventions:

* Decimal everywhere; floats are converted via ``str`` (see :func:`numbers.to_decimal`).
* Rates, margins, returns and yields are **fractions** (``0.0709`` = 7.09%);
  present them with :func:`numbers.format_pct` / :func:`numbers.to_pct`.
* ``None`` = "not reported" (``-`` / ``Nil`` in the document) and propagates; ratios also
  return ``None`` when undefined (zero / non-positive denominator).
* ``ValueError`` = structurally invalid input (negative shares, ``years <= 0``, COE <= g, ...).
* Share counts derived from amounts are floored; display rounding is ROUND_HALF_UP.

Modules: :mod:`numbers`, :mod:`growth`, :mod:`ratios`, :mod:`valuation`, :mod:`ipo`, :mod:`dates`, :mod:`market`.
"""

from finresearch.fincalc import dates, growth, ipo, market, numbers, ratios, valuation
from finresearch.fincalc.numbers import (
    convert,
    format_indian,
    format_inr,
    format_pct,
    parse_number,
    parse_percent,
    round_half_up,
    to_crore,
    to_decimal,
    to_pct,
)

__all__ = [
    "convert",
    "dates",
    "format_indian",
    "format_inr",
    "format_pct",
    "growth",
    "ipo",
    "market",
    "numbers",
    "parse_number",
    "parse_percent",
    "ratios",
    "round_half_up",
    "to_crore",
    "to_decimal",
    "to_pct",
    "valuation",
]
