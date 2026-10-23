"""Corporate-action price factors, cumulative back-adjustment, total return and delisting returns (#369, roadmap P1.7).

A factor f is how many post-action units one pre-action share is worth in price terms: the pre-ex price divided by f
is comparable with the post-ex price. Formulas from the issue (standard exchange/index-provider practice):

    bonus a:b (a new for every b held)      f = (a + b) / b
    split or consolidation                   f = FV_old / FV_new          (consolidation: f < 1)
    rights, n new per N held at P_sub        TERP = (N x P_cum + n x P_sub) / (N + n),  f = P_cum / TERP
    demerger, V = value of the demerged unit f = P_cum / (P_cum - V)
    cumulative factor                        CF_t = product over ex-dates after t of f_i;  adjusted P~_t = P_t / CF_t
    total return                             TR_t = TR_t-1 x (P_t x f_t + D_t) / P_t-1
    delisting                                r = (P_exit + D) / P_last - 1, None if P_exit is unknown

The delisting rule deliberately has no US-style -30 % default for an unknown exit price: unknown stays unknown.
Money stays Decimal, as in the rest of fincalc. Bad terms raise ValueError rather than producing a silent factor 1.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

Event = tuple[date, Decimal]  # (ex-date, factor or dividend per share)


def _pos(**kw: Decimal) -> None:
    for name, v in kw.items():
        if v is None or v <= 0:
            raise ValueError(f"{name} must be positive, got {v}")


def bonus_factor(a: Decimal | int, b: Decimal | int) -> Decimal:
    """Bonus a:b: a new shares for every b held."""
    a, b = Decimal(a), Decimal(b)
    _pos(a=a, b=b)
    return (a + b) / b


def face_value_factor(fv_old: Decimal, fv_new: Decimal) -> Decimal:
    """Split (fv_new < fv_old, f > 1) or consolidation (fv_new > fv_old, f < 1)."""
    _pos(fv_old=fv_old, fv_new=fv_new)
    return Decimal(fv_old) / Decimal(fv_new)


def rights_factor(n_new: Decimal | int, n_held: Decimal | int, p_cum: Decimal, p_sub: Decimal) -> Decimal:
    """Rights n_new for every n_held at p_sub, cum-rights price p_cum. A rights price at or above p_cum has no value
    to the holder (nobody subscribes), so the factor is 1."""
    n_new, n_held, p_cum, p_sub = Decimal(n_new), Decimal(n_held), Decimal(p_cum), Decimal(p_sub)
    _pos(n_new=n_new, n_held=n_held, p_cum=p_cum, p_sub=p_sub)
    if p_sub >= p_cum:
        return Decimal(1)
    terp = (n_held * p_cum + n_new * p_sub) / (n_held + n_new)
    return p_cum / terp


def demerger_factor(p_cum: Decimal, value: Decimal) -> Decimal:
    """Demerger: `value` per share of the demerged business (0 < value < p_cum)."""
    _pos(p_cum=p_cum, value=value)
    if value >= p_cum:
        raise ValueError(f"demerged value {value} must be below the cum price {p_cum}")
    return Decimal(p_cum) / (Decimal(p_cum) - Decimal(value))


def cumulative_factors(days: Sequence[date], events: Sequence[Event]) -> list[Decimal]:
    """CF_t for each day: the product of the factors whose ex-date is after t (same-day factors compound)."""
    out = []
    for t in days:
        cf = Decimal(1)
        for ex, f in events:
            if ex > t:
                cf *= f
        out.append(cf)
    return out


def adjust_prices(days: Sequence[date], prices: Sequence[Decimal], events: Sequence[Event]) -> list[Decimal]:
    """Back-adjusted prices P_t / CF_t: comparable with the latest price."""
    return [p / cf for p, cf in zip(prices, cumulative_factors(days, events), strict=True)]


def total_return_index(days: Sequence[date], prices: Sequence[Decimal], *, factors: Sequence[Event] = (),
                       dividends: Sequence[Event] = ()) -> list[Decimal]:  # fmt: skip
    """TR index starting at 1: TR_t = TR_t-1 x (P_t x f_t + D_t) / P_t-1, with f_t the product of factors and D_t the
    sum of dividends per (pre-action) share whose ex-date is t."""
    f_on: dict[date, Decimal] = {}
    d_on: dict[date, Decimal] = {}
    for ex, f in factors:
        f_on[ex] = f_on.get(ex, Decimal(1)) * f
    for ex, d in dividends:
        d_on[ex] = d_on.get(ex, Decimal(0)) + d
    if not prices:
        return []
    out = [Decimal(1)]
    for i in range(1, len(prices)):
        _pos(price=prices[i - 1])
        t = days[i]
        out.append(out[-1] * (prices[i] * f_on.get(t, Decimal(1)) + d_on.get(t, Decimal(0))) / prices[i - 1])
    return out


def delisting_return(
    p_last: Decimal, p_exit: Decimal | None, *, dividend: Decimal = Decimal(0)
) -> Decimal | None:
    """Return from the last traded close to the exit (offer or liquidation) value; None when the exit is unknown."""
    if p_exit is None:
        return None
    _pos(p_last=p_last)
    return (Decimal(p_exit) + dividend) / Decimal(p_last) - 1


# --------------------------------------------------------------------------- exchange terms -> factors (#369 slice 2)
# Subjects as NSE (/api/corporates-corporateActions `subject`) and BSE (`Purpose`) print them. A subject can carry
# several actions ("Bonus 1:1/Face Value Split ..."): known factors compound; one unknown part makes the ex-date unknown.
_BONUS_T = re.compile(r"\bbonus\b[^0-9]*(\d+)\s*:\s*(\d+)", re.I)
_FV_T = re.compile(r"(split|sub[- ]?division|splt|consolidat\w*).*?(?:from|frm)\s*(?:rs|re)?\.?\s*(\d+(?:\.\d+)?)"
                   r".*?to\s*(?:rs|re)?\.?\s*(\d+(?:\.\d+)?)", re.I | re.S)  # fmt: skip
_RIGHTS_T = re.compile(r"\brights?\b[^0-9@]*?(\d+)\s*:\s*(\d+)", re.I)
_RIGHTS_PREMIUM = re.compile(r"@\s*premium\s*(?:of\s*)?(?:rs|re)?\.?\s*(\d+(?:\.\d+)?)", re.I)
_RIGHTS_PAR = re.compile(r"@\s*par\b", re.I)
_RIGHTS_PRICE = re.compile(r"@\s*(?:rs|re)\.?\s*(\d+(?:\.\d+)?)", re.I)
# the exit price: "@ Rs 450" or "Rs 450 Per Share" (a face value "Rs 10" elsewhere in the subject is not one)
_EXIT = re.compile(
    r"@\s*(?:rs|re|₹)\.?\s*(\d+(?:\.\d+)?)|(?:rs|re|₹)\.?\s*(\d+(?:\.\d+)?)\s*(?:/-)?\s*per\s*share", re.I
)
_DIVIDEND_T = re.compile(r"dividend[^0-9]*?(?:rs\.?|re\.?|₹|inr)\s*-?\s*(\d+(?:\.\d+)?)", re.I)
# (kind, pattern, reason) for actions whose price effect the exchange terms do not state
_UNKNOWN_KINDS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("demerger", re.compile(r"\bde-?merger\b|\bspin[- ]?off\b", re.I),
     "demerger: the value of the demerged business is not in the exchange terms"),
    ("merger", re.compile(r"\bamalgamation\b|\bmerger\b", re.I), "merger/amalgamation: swap ratio not modelled"),
    ("capital_reduction", re.compile(r"\bcapital reduction\b|\breduction (?:of|in) (?:share )?capital\b", re.I),
     "capital reduction: terms not modelled"),
    ("isin_change", re.compile(r"\b(?:change (?:in|of) isin|isin change|new isin)\b", re.I),
     "ISIN change: may carry a share exchange; not modelled"),
    ("scheme", re.compile(r"\bscheme of arrangement\b", re.I), "scheme of arrangement: terms not modelled"),
)  # fmt: skip
# actions that change neither the share count nor the price basis of a price series: factor exactly 1
_NO_EFFECT: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dividend", re.compile(r"\bdividend\b", re.I)),
    ("buyback", re.compile(r"\bbuy[- ]?back\b", re.I)),
    ("meeting", re.compile(r"\b(?:annual|extra[- ]?ordinary) general meeting\b|\bagm\b|\begm\b", re.I)),
    ("interest", re.compile(r"\binterest\b", re.I)),
    ("redemption", re.compile(r"\bredemption\b", re.I)),
    ("distribution", re.compile(r"\bdistribution\b|\bincome distribution\b", re.I)),
)


@dataclass(frozen=True)
class ActionTerms:
    kind: str  # bonus | split | consolidation | rights | demerger | ... | "bonus+split" when compounded
    status: (
        str  # known (factor from the terms) | unknown (reason says why) | none (no price effect: factor 1)
    )
    price_factor: Decimal | None  # f: the pre-ex price divided by f is comparable with the post-ex price
    share_factor: Decimal | None  # shares one old share became without paying (bonus, split, consolidation)
    dividend: Decimal | None  # cash per share, when the subject states it
    reason: str | None
    terms: dict[str, str] = field(default_factory=dict)  # the parsed terms, as strings (stored as JSON)


def _num(x: str | Decimal) -> str:
    return format(Decimal(x).normalize(), "f")


def action_terms(subject: str, *, face_value: Decimal | None = None, p_cum: Decimal | None = None,
                 dividend: Decimal | None = None) -> ActionTerms:  # fmt: skip
    """Price and share factor of one exchange corporate-action subject. `face_value` (NSE's `faceVal`) prices a
    rights issue "@ Premium Rs X" (P_sub = FV + X); `p_cum` is the close on the last cum-rights day. Terms the exchange
    does not state (a demerged value, a bare "Consolidation Of Shares", a rights issue with no ratio or price) give
    status "unknown" with a reason and no factor: never a silent 1."""
    s = " ".join((subject or "").split())
    kinds: list[str] = []
    f = sh = Decimal(1)
    terms: dict[str, str] = {}
    unknown: list[tuple[str, str]] = []
    try:
        if m := _BONUS_T.search(s):
            f *= (b := bonus_factor(int(m.group(1)), int(m.group(2))))
            sh *= b
            kinds.append("bonus")
            terms |= {"bonus_new": m.group(1), "bonus_held": m.group(2)}
        if m := _FV_T.search(s):
            fv = face_value_factor(Decimal(m.group(2)), Decimal(m.group(3)))
            f, sh = f * fv, sh * fv
            kinds.append("consolidation" if fv < 1 else "split")
            terms |= {"fv_old": _num(m.group(2)), "fv_new": _num(m.group(3))}
        elif re.search(r"\bconsolidat", s, re.I):
            unknown.append(("consolidation", "consolidation: face values not in the terms"))
        elif re.search(r"\bsplit\b|\bsub[- ]?division\b", s, re.I):
            unknown.append(("split", "split: face values not in the terms"))
        if re.search(r"\brights?\b", s, re.I):
            got = _rights(s, face_value, p_cum)
            if isinstance(got, str):
                unknown.append(("rights", got))
            else:
                f *= got[0]
                terms |= got[1]
                kinds.append("rights")
    except ValueError as e:
        return ActionTerms("bad_terms", "unknown", None, None, dividend, f"bad terms in {s!r}: {e}", {})
    for kind, rx, why in _UNKNOWN_KINDS:
        if rx.search(s):
            unknown.append((kind, why))
    if re.search(r"\bdelist", s, re.I):
        return _delisting(s, dividend)
    div = dividend if dividend is not None else _dividend(s)
    if unknown:
        return ActionTerms("+".join([*kinds, *(k for k, _ in unknown)]), "unknown", None, None, div,
                           "; ".join(w for _, w in unknown), terms)  # fmt: skip
    if kinds:
        return ActionTerms("+".join(kinds), "known", f, sh, div, None, terms)
    for kind, rx in _NO_EFFECT:
        if rx.search(s):
            return ActionTerms(kind, "none", Decimal(1), Decimal(1), div, None, {})
    return ActionTerms("unknown", "unknown", None, None, div, f"unrecognised action terms: {s!r}", {})


def _rights(
    s: str, face_value: Decimal | None, p_cum: Decimal | None
) -> tuple[Decimal, dict[str, str]] | str:
    """(factor, terms) of "Rights n:N @ Premium Rs X" / "@ Par" / "@ Rs P", or the reason it is unknown. NSE prints
    the ratio new:held, as for a bonus."""
    if not (m := _RIGHTS_T.search(s)):
        return "rights: the ratio is not in the terms"
    n_new, n_held = m.group(1), m.group(2)
    if pm := _RIGHTS_PREMIUM.search(s):
        if face_value is None:
            return "rights: priced at a premium, but the face value is not known"
        p_sub = Decimal(face_value) + Decimal(pm.group(1))
    elif _RIGHTS_PAR.search(s):
        if face_value is None:
            return "rights: priced at par, but the face value is not known"
        p_sub = Decimal(face_value)
    elif pm := _RIGHTS_PRICE.search(s):
        p_sub = Decimal(pm.group(1))
    else:
        return "rights: the subscription price is not in the terms"
    if p_cum is None:
        return "rights: the cum-rights close is not known"
    terms = {"n_new": n_new, "n_held": n_held, "p_sub": _num(p_sub), "p_cum": _num(p_cum)}
    return rights_factor(int(n_new), int(n_held), Decimal(p_cum), p_sub), terms


def _delisting(s: str, dividend: Decimal | None) -> ActionTerms:
    """A delisting has no price factor: its terms are the exit price (fincalc delisting_return), unknown when the
    subject does not state one."""
    if (m := _EXIT.search(s)) and not re.search(r"face value", s[: m.start()][-30:], re.I):
        price = m.group(1) or m.group(2)
        return ActionTerms("delisting", "known", None, None, dividend, None, {"exit_price": _num(price)})
    return ActionTerms(
        "delisting", "unknown", None, None, dividend, "delisting: exit price not in the terms", {}
    )


def _dividend(s: str) -> Decimal | None:
    amounts = _DIVIDEND_T.findall(s)
    return sum((Decimal(a) for a in amounts), Decimal(0)).normalize() if amounts else None
