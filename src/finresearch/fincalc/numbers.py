"""Parsing Indian-formatted document numbers, unit conversion and INR formatting.

Conventions used across ``finresearch.fincalc``:

* All arithmetic is :class:`~decimal.Decimal`. Inputs may be ``int``, ``str`` or ``Decimal``;
  ``float`` is accepted but converted via ``Decimal(str(x))`` (never ``Decimal(float)``) so
  ``0.1`` stays ``0.1``.
* Ratios and percentages are returned as **fractions** (``0.0709`` means 7.09%). Use
  :func:`to_pct` / :func:`format_pct` to present them.
* ``None`` means "not reported" (the RHP printed ``-`` / ``Nil``); it propagates through
  ratio functions rather than being treated as zero.
* Rounding for presentation is ``ROUND_HALF_UP`` (how figures are printed in offer documents),
  never the Decimal default banker's rounding.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal

type Num = Decimal | int | str | float
type Unit = Literal["inr", "thousand", "lakh", "million", "crore", "billion"]

#: Power of ten each unit represents, in rupees. 1 crore = 10 million = 100 lakh.
UNIT_EXPONENT: dict[str, int] = {
    "inr": 0,
    "thousand": 3,
    "lakh": 5,
    "million": 6,
    "crore": 7,
    "billion": 9,
}

_UNIT_ALIASES: dict[str, str] = {
    "inr": "inr",
    "rs": "inr",
    "rupee": "inr",
    "rupees": "inr",
    "₹": "inr",
    "thousand": "thousand",
    "thousands": "thousand",
    "k": "thousand",
    "lakh": "lakh",
    "lakhs": "lakh",
    "lac": "lakh",
    "lacs": "lakh",
    "million": "million",
    "millions": "million",
    "mn": "million",
    "crore": "crore",
    "crores": "crore",
    "cr": "crore",
    "billion": "billion",
    "billions": "billion",
    "bn": "billion",
}

#: Tokens that offer documents print for "no value".
NULL_TOKENS = frozenset({"", "-", "–", "—", "nil", "na", "n.a.", "n/a", "none", "not applicable"})

_CURRENCY_RE = re.compile(r"(₹|rs\.?|inr)", re.IGNORECASE)


def to_decimal(x: Num) -> Decimal:
    """Coerce ``x`` to Decimal. Floats go via ``str`` to avoid binary noise.

    Raises ValueError for bools, NaN/Infinity and unparseable strings.
    """
    if isinstance(x, bool):
        raise ValueError("bool is not a number")
    if isinstance(x, Decimal):
        d = x
    elif isinstance(x, int):
        d = Decimal(x)
    elif isinstance(x, float):
        d = Decimal(str(x))
    elif isinstance(x, str):
        try:
            d = Decimal(x.strip())
        except InvalidOperation as e:
            raise ValueError(f"not a number: {x!r}") from e
    else:
        raise ValueError(f"unsupported numeric type: {type(x).__name__}")
    if not d.is_finite():
        raise ValueError(f"non-finite number: {x!r}")
    return d


def require_shares(x: Num, name: str = "shares", *, allow_zero: bool = False) -> Decimal:
    """Validate a whole, non-negative share count (``> 0`` unless ``allow_zero``); ValueError otherwise."""
    d = to_decimal(x)
    if d < 0 or (d == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'>= 0' if allow_zero else '> 0'}, got {d}")
    if d != d.to_integral_value():
        raise ValueError(f"{name} must be a whole number, got {d}")
    return d


def require_price(x: Num, name: str = "price") -> Decimal:
    """Validate a strictly positive price; ValueError otherwise."""
    d = to_decimal(x)
    if d <= 0:
        raise ValueError(f"{name} must be > 0, got {d}")
    return d


def opt_decimal(x: Num | None) -> Decimal | None:
    """Like :func:`to_decimal` but passes ``None`` through."""
    return None if x is None else to_decimal(x)


def parse_number(text: str | None) -> Decimal | None:
    """Parse a number as printed in an RHP/annual report.

    Handles Indian grouping (``"1,17,64,705"``), western grouping (``"11,716.54"``),
    accounting negatives (``"(84.17)"``), leading minus, currency (``"₹ 272"``, ``"Rs. 10"``),
    a trailing ``%`` (``"0.94%"`` -> ``Decimal("0.94")``: the *printed* value, not a fraction —
    use :func:`parse_percent` for a fraction) and scientific notation (``"2.32524175E8"``).

    Returns ``None`` for null tokens (``-``, ``—``, ``Nil``, ``NA``, empty). Raises ValueError
    for anything else that is not a number.
    """
    if text is None:
        return None
    s = text.strip()
    if s.lower() in NULL_TOKENS:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    s = _CURRENCY_RE.sub("", s).strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    s = s.replace(",", "").replace(" ", "").replace(" ", "")
    # Unicode minus / dashes used as a leading minus sign.
    if s[:1] in {"−", "–", "—"} and len(s) > 1:
        s = "-" + s[1:]
    if s.lower() in NULL_TOKENS:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"cannot parse number from {text!r}") from e
    if not d.is_finite():
        raise ValueError(f"non-finite number: {text!r}")
    if negative:
        if d < 0:
            raise ValueError(f"double negative in {text!r}")
        d = -d
    return d


def parse_percent(text: str | None) -> Decimal | None:
    """Parse ``"0.94%"`` / ``"0.94"`` (printed percent points) into a fraction ``0.0094``."""
    d = parse_number(text)
    return None if d is None else d / 100


def normalize_unit(unit: str) -> str:
    """Map aliases such as ``"cr"``, ``"mn"``, ``"lakhs"``, ``"₹"`` to a canonical unit name."""
    key = unit.strip().lower().rstrip(".")
    if key.startswith("₹ "):
        key = key[2:]
    try:
        return _UNIT_ALIASES[key]
    except KeyError as e:
        raise ValueError(f"unknown unit: {unit!r}") from e


def convert(value: Num | None, from_unit: str, to_unit: str) -> Decimal | None:
    """Convert an amount between rupee units exactly (no rounding).

    ``convert("11716.54", "million", "crore") == Decimal("1171.654")``.
    Units: inr, thousand, lakh (1e5), million (1e6), crore (1e7), billion (1e9).
    """
    if value is None:
        return None
    shift = UNIT_EXPONENT[normalize_unit(from_unit)] - UNIT_EXPONENT[normalize_unit(to_unit)]
    return to_decimal(value).scaleb(shift)


def to_crore(value: Num | None, from_unit: str = "inr") -> Decimal | None:
    """Shorthand for ``convert(value, from_unit, "crore")``."""
    return convert(value, from_unit, "crore")


def to_rupees(value: Num | None, from_unit: str) -> Decimal | None:
    """Shorthand for ``convert(value, from_unit, "inr")``."""
    return convert(value, from_unit, "inr")


def round_half_up(value: Num, decimals: int = 2) -> Decimal:
    """Round to ``decimals`` places with ROUND_HALF_UP (document convention)."""
    q = Decimal(1).scaleb(-decimals)
    return to_decimal(value).quantize(q, rounding=ROUND_HALF_UP)


def group_indian(integer_digits: str) -> str:
    """Insert Indian grouping commas into a string of digits: ``"11764705"`` -> ``"1,17,64,705"``."""
    if not integer_digits.isdigit():
        raise ValueError(f"expected digits only: {integer_digits!r}")
    if len(integer_digits) <= 3:
        return integer_digits
    head, tail = integer_digits[:-3], integer_digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join([*groups, tail])


def format_indian(value: Num, decimals: int = 2) -> str:
    """Format a plain number with Indian grouping and ROUND_HALF_UP: ``1,17,64,705.00``."""
    d = round_half_up(value, decimals)
    sign = "-" if d < 0 else ""
    text = f"{abs(d):f}"
    int_part, _, frac = text.partition(".")
    out = sign + group_indian(int_part)
    return f"{out}.{frac}" if decimals > 0 else out


def format_inr(
    value: Num | None,
    unit: str = "cr",
    decimals: int = 2,
    *,
    from_unit: str = "inr",
    symbol: bool = True,
    suffix: bool = True,
) -> str:
    """Format a rupee amount (given in ``from_unit``) in ``unit`` with Indian grouping.

    ``format_inr(59847863112) == "₹5,984.79 cr"``. ``None`` renders as ``"-"``.
    """
    if value is None:
        return "-"
    canon = normalize_unit(unit)
    converted = convert(value, from_unit, canon)
    assert converted is not None
    body = format_indian(converted, decimals)
    labels = {
        "inr": "",
        "thousand": " thousand",
        "lakh": " lakh",
        "million": " mn",
        "crore": " cr",
        "billion": " bn",
    }
    neg = body.startswith("-")
    body = body.lstrip("-")
    out = ("₹" if symbol else "") + body + (labels[canon] if suffix else "")
    return "-" + out if neg else out


def to_pct(fraction: Num | None, decimals: int = 2) -> Decimal | None:
    """Fraction -> percent points, rounded half-up: ``0.070871`` -> ``Decimal("7.09")``."""
    if fraction is None:
        return None
    return round_half_up(to_decimal(fraction) * 100, decimals)


def format_pct(fraction: Num | None, decimals: int = 2) -> str:
    """Fraction -> ``"7.09%"``; ``None`` -> ``"-"``."""
    p = to_pct(fraction, decimals)
    return "-" if p is None else f"{p:f}%"
