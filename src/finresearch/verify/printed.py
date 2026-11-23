"""Printed-value check of a synthesised report: the figure printed beside a citation is the cited claim's value.

`verify.gate.check_report` proves that every `[C<id>]` is a usable claim; this module proves the text agrees with
it (issue #502; the idea of a number-consistency pass over the finished document is after the ib-check-deck and
initiating-coverage skills of anthropics/financial-services, rewritten). It reads only the report and the ledger
rows the gate already loaded, uses no model, and decides nothing about severity (`verify.gate.printed_findings`).

How one citation is judged
--------------------------
* Group  — consecutive citations (`[C1][C2]`, `[C1], [C2]`, `[C1] and [C2]`) form one group and share the figures
           printed before them ("Only ₹3,976.54m of ₹6,847.20m [C4688][C4687]": two figures, two claims, any order).
* Scope  — what may SATISFY a claim: in a table row the cell that holds the citation, or the whole row when that
           cell prints no figure of its own (a "Source" column); elsewhere the sentence.
           What may be ACCUSED (reported as "printed instead"): only the clause between the previous citation group
           of that scope and this one, so a figure that already has its own citation is never held against the next.
* Pass   — any number in the scope equals the claim's value (`matches`: display rounding, ₹ scales, % vs fraction,
           the printed sign), or equals another figure of the claim's own statement or period. Ledger claims are
           often one statement holding several periods with one headline `value` ("38.54% (Q1 FY27), 25.75%
           (FY26), ..."), so a report row printing the FY25 figure beside that claim is supported by it.
* Not judged — a claim without a numeric value; a claim cited as the INPUT of a computation ("fincalc from
           [C1][C2]", "inputs [C1]"); a clause labelled as a computation ("(fincalc)", "analyst estimate"); a scope
           that prints no figure that could be the claim's (none at all, only other cited claims' figures, or only
           figures of another kind: a "33%" is never held against a claim in ₹ million).
* Flag   — otherwise: the claim's value is not printed, and the clause prints a figure of the claim's kind that no
           claim cited in the scope holds. The finding names the claim, the value held and the figure printed.

Display rounding (the precise rule, `matches`)
----------------------------------------------
A printed figure p with d digits after the point equals a held value v when |p - v| <= half a unit of p's last
printed digit (2,651.97 -> "2,652", "2,652.0"; 0.057377 -> "0.057", "0.06"), or within `REL_TOL` (0.05 %, the
gate's tolerance for source figures). The half-unit rule applies in the unit the figure is printed in:
"₹2.65k crore" is 2,650 crore +- 5 crore, "₹26.5bn" is 26.5bn +- 0.05bn. It does not apply to
* a bare integer with no marker (no ₹, %, x, scale word, decimal point or digit grouping, and not followed by the
  claim's own unit word): "3 plants" is 3, never a rounded 2.65;
* a ₹ scale the figure does not name: an unmarked "2,652" may be the claim's value in its own unit after rounding,
  or its value in another ₹ scale within `REL_TOL` only (so "3" is never 2.65 billion);
* a printed 0, which equals only a held 0.
A scale word or a % sign next to the figure is binding: "₹2,651.97 crore" is not 2,651.97 million, and "0.15%" is
not 15 % (an unmarked "0.15" may be the fraction form of 15 %).

Sign: a matched magnitude is a `sign` mismatch only on positive evidence of the opposite sign (an explicit + or
minus, an accounting bracket, or a profit / loss word next to the figure, read as `verify.values.sign_conflict`
does). A negative claim printed without any sign word ("₹348.45m higher than the books") passes.

Also reported, separately: one metric and period printed with two values (`TwoValues`): two different cited
claims with the same metric and period whose headline values are both printed and more than `CONFLICT_TOL` apart.
One claim printing several figures of its own statement is not a finding.

Not read: a unit declared only in a table caption or header ("(₹ million)"), citations in a non-canonical spelling
("(C12)"; the gate asks for those to be rewritten), and which of two claims cited in one sentence owns which figure
(two ids swapped inside one sentence both pass; in a table with one citation per cell they are caught).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from finresearch.verify.gate import CONFLICT_TOL, REL_TOL, candidate_forms, rupee_scale
from finresearch.verify.values import POS_RE, near_polarity, polarity

__all__ = ["Fig", "Finding", "PrintedReport", "TwoValues", "check_printed", "figures", "matches"]

# --------------------------------------------------------------------------- figures printed in report text
_MON = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
        r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")  # fmt: skip
_YEAR = r"(?:19|20)\d{2}"
# Digits that are not figures: citations, times, dates and period labels. A 4-digit year inside a date stays
# readable (group y*: a claim may hold a year), everything else is blanked before the numbers are read.
_NOT_FIGURES = re.compile(
    r"\[C\d+\]"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"  # 16:09, 17:00:00
    rf"|\b(?P<y1>{_YEAR})-\d{{2}}-\d{{2}}\b|\b\d{{1,2}}/\d{{1,2}}/\d{{2,4}}\b"  # 2026-09-28, 28/09/2026
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?[-\s]{_MON}\b(?:[-\s,]+(?:(?P<y2>{_YEAR})|\d{{2}})\b)?"  # 28-Sep-2026, 29 Sep
    rf"|\b{_MON}[-\s,]+(?:\d{{1,2}},?\s+)?(?P<y3>{_YEAR})\b|\b{_MON}-\d{{2}}\b"  # Jan-2025, March 31, 2025, Jun-26
    r"|\b(?:FY|CY|AY)\s?'?\d{2,4}(?:\s?[-–/]\s?\d{2,4})?\b"  # FY26, FY24–26, FY2020-21
    rf"|\b{_YEAR}\s?[-–/]\s?\d{{2}}\b"  # 2025-26
)
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CUR_BEFORE = re.compile(r"(₹|\brs\.?|\binr|us\$|\busd|\$|€|£)\s?$", re.I)
_CUR = {"₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "us$": "USD", "usd": "USD", "$": "USD"}
# scale words after a figure; the one-letter forms count only when glued to a currency figure ("₹915m", "$2.5b")
_SCALE_AFTER = re.compile(
    r"\s?(lakh\s+crores?|crores?|cr\b\.?|lakhs?|lacs?|millions?|mn\b|billions?|bn\b|trillions?|thousands?)"
    r"|(k|m|b)(?![a-z])",
    re.I,
)
_SCALES = {"lakh crore": 12, "crore": 7, "cr": 7, "lakh": 5, "lac": 5, "million": 6, "mn": 6, "m": 6, "billion": 9,
           "bn": 9, "b": 9, "trillion": 12, "thousand": 3, "k": 3}  # fmt: skip
_PCT_AFTER = re.compile(r"\s?(%|per\s?cent\b|percent\b|percentage points?\b|pp\b|ppts?\b)", re.I)
_MULT_AFTER = re.compile(r"(x\b|×(?!\s?[\d₹])|\s?times\b)", re.I)
_WORD_AFTER = re.compile(r"[\s*_]*([A-Za-z][A-Za-z/-]*)")


def _blank(text: str) -> str:
    """The text with everything `_NOT_FIGURES` names replaced by spaces (positions are kept)."""

    def sub(m: re.Match) -> str:
        out = [" "] * (m.end() - m.start())
        for name in ("y1", "y2", "y3"):
            if m.group(name):
                a = m.start(name) - m.start()
                out[a : a + 4] = m.group(name)
        return "".join(out)

    return _NOT_FIGURES.sub(sub, text)


def _scale_of(word: str) -> Decimal:
    w = re.sub(r"\s+", " ", word.lower().rstrip("."))
    return Decimal(10) ** _SCALES[w if w in _SCALES else w.rstrip("s")]


@dataclass(frozen=True)
class Fig:
    """One number as the report prints it."""

    value: Decimal  # the magnitude, as printed (no scale applied)
    decimals: int  # digits printed after the point
    neg: bool  # "-12.5", "−₹2.36", "₹(117.64)m", or a bracket that is the whole table cell
    plus: bool  # an explicit "+"
    cur: str | None  # INR | USD | OTHER, when a currency sign is printed
    scale: Decimal | None  # the printed scale word, in units ("crore" -> 10^7); None when there is none
    kind: str  # pct ("%", "percentage points") | mult ("x", "times") | plain
    figure: bool  # reads as a figure (a marker, a decimal point or digit grouping), not as a bare integer
    start: int  # the span in the scope's text, markers included
    end: int
    text: str
    word: str = ""  # the word that follows ("shares", "days"), lower case

    @property
    def base(self) -> Decimal:
        return self.value * (self.scale or 1)

    @property
    def half(self) -> Decimal:
        """Half a unit of the last printed digit, in the figure's own unit."""
        return Decimal(5).scaleb(-(self.decimals + 1))


def _lead(t: str, a: int) -> tuple[int, str | None, bool, bool, bool, bool]:
    """Walk left from a number over its sign, currency and bracket, in any order ("−₹2.36", "₹(117.64)", "(₹5)").
    Returns (start, currency, negative, plus, bracketed, the currency stands outside the bracket)."""
    i, cur, neg, plus, bracket, cur_outside = a, None, False, False, False, False
    for _ in range(4):
        if i and t[i - 1] == "(" and not bracket:
            bracket, i = True, i - 1
        elif (cm := _CUR_BEFORE.search(t[:i])) and cur is None:
            cur, i, cur_outside = _CUR.get(cm.group(1).lower(), "OTHER"), cm.start(), bracket
        elif i and t[i - 1] in "+-−–" and not (neg or plus):
            before = t[: i - 1]
            prev = before.rstrip()
            gap = len(before) - len(prev)
            if t[i - 1] == "+":
                plus = True
            elif t[i - 1] == "−" or not prev:
                neg = True
            else:  # a hyphen or dash joins two things: "258–272", "29.84x–43.99x", "top-10", "10 - 12"
                glued = gap == 0 and (prev[-1].isalnum() or prev[-1] in "%)]")
                neg = not (glued or (gap == 1 and (prev[-1].isdigit() or prev[-1] == "%")))
            if not (neg or plus):
                break
            i -= 1
        else:
            break
    return i, cur, neg, plus, bracket, cur_outside


def figures(text: str, *, cell: bool = False) -> list[Fig]:
    """The numbers a piece of report text prints, with their sign and markers; dates, times, period labels,
    citations and digits glued to letters ("Q1", "CAT6A", "1Lattice") are not numbers. `cell`: the text is one
    whole table cell, where a bracket around its only content is an accounting negative ("(117.64)")."""
    t = _blank(text)
    out: list[Fig] = []
    for m in _NUM.finditer(t):
        raw = m.group(0).rstrip(",")
        a, b = m.start(), m.start() + len(raw)
        start, cur, neg, plus, bracket, cur_outside = _lead(t, a)
        if start == a and a and (t[a - 1].isalnum() or (t[a - 1] in "./" and a > 1 and t[a - 2].isdigit())):
            continue
        if re.match(r"[./]\d", t[b : b + 2]):  # "2.4.2", "25/-" is fine but "3/4" is not a figure
            continue
        try:
            value = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            continue
        decimals = len(raw.partition(".")[2])
        grouped = "," in raw
        end, kind, scale = b, "plain", None
        closed = bracket and t[b:].lstrip(" %x").startswith(")")
        if pm := _PCT_AFTER.match(t[b:]):
            end, kind = b + pm.end(), "pct"
        elif mm := _MULT_AFTER.match(t[b:]):
            end, kind = b + mm.end(), "mult"
        if closed:
            end = t.index(")", end) + 1
        if kind == "plain":
            sm = _SCALE_AFTER.match(t[end:])
            if sm and (sm.group(1) or cur or decimals or grouped):
                end, scale = end + sm.end(), _scale_of(sm.group(1) or sm.group(2))
            elif t[end : end + 1].isalpha():  # "5G", "3D", "500m" of cable: not a figure
                continue
        if bracket and not closed:  # "(₹75, 28-Sep ...": an opening bracket of prose, not of this number
            start += t[start:a].count("(")
        whole = cell and not t[:start].strip(" *_~") and not t[end:].strip(" *_~")
        wm = _WORD_AFTER.match(t[end:])
        out.append(Fig(value=value, decimals=decimals, neg=neg or (closed and (cur_outside or whole)), plus=plus,
                       cur=cur, scale=scale, kind=kind,
                       figure=bool(cur or scale or decimals or grouped or kind != "plain"), start=start, end=end,
                       text=text[start:end].strip(), word=wm.group(1).lower() if wm else ""))  # fmt: skip
    return out


# --------------------------------------------------------------------------- what a claim holds
_U_PCT = re.compile(r"%|per\s?cent|percent|\bpct\b|percentage", re.I)
_U_FRAC = re.compile(r"\b(?:fraction|ratio)\b", re.I)
_U_MULT = re.compile(r"^\s*(?:x\b|times\b|multiple)", re.I)
_U_USD = re.compile(r"us\$|\busd\b|\$|dollar", re.I)
_U_OTHER_CUR = re.compile(r"€|£|¥|\b(?:eur|gbp|jpy|euro)\b", re.I)
_U_INR = re.compile(r"₹|\b(?:inr|rs|rupees?)\b", re.I)
_U_SCALE = re.compile(r"lakh\s+crores?|crores?|\bcr\b|lakhs?|lacs?|millions?|\bmn\b|billions?|\bbn\b|trillions?|"
                      r"thousands?", re.I)  # fmt: skip
_U_WORD = re.compile(r"[a-z]{2,}")


def _unit(unit: str | None) -> tuple[str, str | None, Decimal, frozenset[str]]:
    """(kind, currency, scale, unit words) of a claim's unit. Kinds: money | pct | frac | mult | other."""
    u = unit or ""
    sm = _U_SCALE.search(u)
    scale = _scale_of(sm.group(0)) if sm else Decimal(1)
    cur = "USD" if _U_USD.search(u) else "OTHER" if _U_OTHER_CUR.search(u) else None
    if cur is None and (_U_INR.search(u) or rupee_scale(unit) is not None):
        cur = "INR"
    if _U_PCT.search(u):
        kind = "pct"
    elif cur:
        kind = "money"
    elif _U_FRAC.search(u):
        kind = "frac"
    else:
        kind = "mult" if _U_MULT.search(u) else "other"
    words = {w for w in _U_WORD.findall(u.lower())} if kind == "other" else set()
    return kind, cur, scale, frozenset(words | {w + "s" for w in words} | {w.rstrip("s") for w in words})


def _near(p: Decimal, v: Decimal, tol: Decimal) -> bool:
    return abs(p - v) <= max(tol, abs(v) * REL_TOL)


def matches(f: Fig, value: Decimal, unit: str | None) -> bool:
    """Is the printed figure the held value (magnitudes; the sign is judged separately)? The module docstring
    states the rule: display rounding in the unit the figure names, a binding scale word or % sign, and for an
    unmarked figure the claim's other ₹ scales within REL_TOL only."""
    kind, cur, scale, words = _unit(unit)
    v = abs(value)
    if f.value == 0 or v == 0:
        return f.value == v
    half = f.half if f.figure or f.word in words else Decimal(0)
    if f.cur and cur and f.cur != cur:
        return False
    if f.scale is not None:
        return kind in ("money", "other") and _near(f.base, v * scale, half * f.scale)
    if f.kind == "pct":
        if kind == "frac":
            return _near(f.value, v * 100, half)
        return kind in ("pct", "other") and _near(f.value, v, half)
    if f.kind == "mult":
        if kind == "pct":
            return _near(f.value * 100, v, half * 100)
        return kind in ("mult", "frac", "other") and _near(f.value, v, half)
    if _near(f.value, v, half):
        return True
    if kind == "pct":
        return _near(f.value, v / 100, half)
    if kind == "frac":
        return _near(f.value, v * 100, half)
    return kind == "money" and any(_near(f.value, abs(alt), Decimal(0)) for alt in candidate_forms(v, unit))


def _same(p: Fig, s: Fig) -> bool:
    """A printed figure equals a figure of a claim's own statement (same rounding rule; markers must not clash)."""
    if p.value == 0 or s.value == 0:
        return p.value == s.value
    if (p.kind != s.kind and "plain" not in (p.kind, s.kind)) or (p.cur and s.cur and p.cur != s.cur):
        return False
    half = p.half if p.figure else Decimal(0)
    if _near(p.value, s.value, half) and (p.scale is None or s.scale is None or p.scale == s.scale):
        return True
    return p.scale is not None and s.scale is not None and _near(p.base, s.base, half * p.scale)


@dataclass
class _Held:
    """A cited claim as this check reads it."""

    id: int
    value: Decimal | None
    unit: str | None
    kind: str
    cur: str | None
    words: frozenset[str]
    own: list[Fig]  # the figures of its own statement and period
    neg: bool  # the value, or its figure in the statement, is negative
    word: int  # polarity of the words next to its figure in the statement, else of the metric name
    metric: str
    period: str
    importance: str | None

    def holds(self) -> str:
        """ "2,651.97 INR million (pat, FY2026)": what the finding tells the synthesiser the claim holds."""
        what = ", ".join(x for x in (self.metric, self.period) if x)
        return f"{_plain(self.value)} {self.unit or ''}".strip() + (f" ({what})" if what else "")


def _plain(v: Decimal | None) -> str:
    if v is None:
        return "no value"
    s = f"{v.normalize():,f}" if v == v.to_integral() else f"{v:,f}".rstrip("0").rstrip(".")
    return s


def _held(c: Any) -> _Held:
    """Read a ledger row (a `Claim`, or anything with its attributes). A claim is judged only when it has a value."""
    value = Decimal(c.value) if c.value is not None else None
    kind, cur, _scale, words = _unit(c.unit)
    statement = c.statement or ""
    own = figures(statement) + figures(c.period or "")
    neg, word = bool(value is not None and value < 0), 0
    if value is not None:
        for f in figures(statement):
            if matches(f, value, c.unit):
                neg = neg or f.neg
                word = near_polarity(statement[: f.start][-80:], statement[f.end :])
                break
    word = word or polarity((c.metric or "").replace("_", " "))
    return _Held(id=c.id, value=value, unit=c.unit, kind=kind, cur=cur, words=words, own=own, neg=neg, word=word,
                 metric=c.metric or "", period=c.period or "", importance=c.importance)  # fmt: skip


def _fits(f: Fig, h: _Held) -> bool:
    """May this printed figure be held against the claim: a figure, and of the claim's kind?"""
    if f.cur and h.cur and f.cur != h.cur:
        return False
    if f.kind == "pct":
        return h.kind in ("pct", "frac")
    if f.kind == "mult":
        return h.kind in ("mult", "frac")
    if f.cur:
        return h.kind == "money"
    if h.kind == "other":  # a count: digit grouping, a decimal, a scale word, or its own unit word after it
        return f.figure or f.word in h.words
    return f.figure and (f.scale is None or h.kind == "money")


# --------------------------------------------------------------------------- where a citation sits
_ID = re.compile(r"\[C(\d+)\]")
_GROUP = re.compile(r"\[C\d+\](?:\s*(?:,|and\b|&)?\s*\[C\d+\])*")
_ROW_RULE = re.compile(r"^\s*:?-{2,}:?\s*$")
# the end of a sentence: its stop, closing marks, any citations trailing it, then a capital, digit or ₹
_SENT_END = re.compile(r"[.!?][\"')\]*_]*(?:\s*\[C\d+\])*\s+(?=[\"'(*_\[]*[A-Z0-9₹])")
_ABBREV = re.compile(r"(?:\b(?:e\.g|i\.e|vs|approx|Rs|No|Nos|Ltd|Inc|Co|Corp|Comm|est|cf|Dr|Mr|Mrs|Ms|St|incl|excl|viz|"
                     r"Pvt|Jr|Sr)|\b[A-Z])$")  # fmt: skip
_LIST_MARK = re.compile(r"^\s*(?:#+\s*)?(?:[-*+]\s+)?(?:\d{1,2}[.)]\s+)?(?:\[[ xX]\]\s+)?")
# the citations that follow are inputs of a computed figure, not its source
_INPUT = re.compile(r"(?:\bfrom|\binputs?|\busing)\s*:?\s*$", re.I)
_COMPUTED = re.compile(r"\bfincalc\b|\bcomput(?:ed|ation)\b|\bcalculat(?:ed|ion)\b|\bderived\b|"
                       r"\banalyst(?:'s)? (?:estimate|summation|calculation)\b", re.I)  # fmt: skip


@dataclass
class _Seen:
    """A printed number in its place: the words around it (for its sign) and whether it is labelled as computed."""

    fig: Fig
    before: str
    after: str
    label: str = ""  # the row label of a table row
    computed: bool = False


@dataclass
class _Site:
    """One citation group and the text it is judged against."""

    line: int
    ids: list[int]
    wide: list[_Seen]  # may satisfy a claim of the group
    near: list[_Seen]  # may be reported as printed instead
    cited: set[int]  # every claim cited in the wide scope
    is_input: bool
    text: str


def _seen(text: str, *, cell: bool = False, label: str = "", computed: bool = False) -> list[_Seen]:
    return [
        _Seen(f, text[: f.start][-80:], text[f.end :][:40], label, computed) for f in figures(text, cell=cell)
    ]


def _sentences(text: str) -> Iterator[tuple[int, int]]:
    start = 0
    for m in _SENT_END.finditer(text):
        if _ABBREV.search(text[start : m.start()]):
            continue  # "Rs. 258", "approx. 12", an initial
        yield start, m.end()
        start = m.end()
    yield start, len(text)


def _in_text(
    line_no: int, text: str, computed: list[Fig], *, cell: bool = False, label: str = ""
) -> Iterator[_Site]:
    """Sites of a paragraph, list item or table cell that prints its own numbers, sentence by sentence. Figures of
    a clause labelled as a computation are added to `computed`."""
    for a, b in _sentences(text):
        sent = text[a:b]
        whole = cell and not text[:a].strip() and not text[b:].strip()
        every = _seen(sent, cell=whole, label=label)
        cited = {int(x) for x in _ID.findall(sent)}
        prev = 0
        for g in [*_GROUP.finditer(sent), None]:
            stop = g.start() if g else len(sent)
            clause = sent[prev:stop]
            is_computed = bool(_COMPUTED.search(clause))
            is_input = bool(g and _INPUT.search(clause)) and (is_computed or "input" in clause.lower()[-12:])
            near = [_Seen(s.fig, s.before, s.after, label, is_computed) for s in every
                    if prev <= s.fig.start and s.fig.end <= stop]  # fmt: skip
            if is_computed or is_input:
                computed += [s.fig for s in near if s.fig.figure]
            if g is None:
                break
            yield _Site(line_no, [int(x) for x in _ID.findall(g.group(0))], every, near, cited, is_input,
                        " ".join(sent.split()))  # fmt: skip
            prev = g.end()


def _cells(line: str) -> list[str] | None:
    """The cells of a markdown table row; None for other lines and for the |---| rule."""
    s = line.strip()
    if not s.startswith("|"):
        return None
    cells = re.split(r"(?<!\\)\|", s.strip("|"))
    return None if all(_ROW_RULE.match(c) for c in cells) else cells


def _in_row(line_no: int, line: str, cells: list[str], computed: list[Fig]) -> Iterator[_Site]:
    """Sites of a table row. A cell that prints numbers is its own scope. A citation standing alone in a cell
    without numbers (a "Source" column) covers the figures of the whole row; one that follows words in such a cell
    ("QIB stays thin [C920]") supports those words and has no figure to be judged against."""
    label = cells[0]
    in_label = bool(_COMPUTED.search(label))
    row = [
        s
        for c in cells
        for s in _seen(c, cell=True, label=label, computed=in_label or bool(_COMPUTED.search(c)))
    ]
    cited = {int(x) for x in _ID.findall(line)}
    text = " ".join(line.split())
    direct = inputs = False
    for c in cells:
        if not _ID.search(c):
            continue
        if figures(c):
            direct = True
            yield from _in_text(line_no, c, computed, cell=True, label=label)
            continue
        prev = 0
        for g in _GROUP.finditer(c):
            clause = c[prev : g.start()]
            prev = g.end()
            ids = [int(x) for x in _ID.findall(g.group(0))]
            if _INPUT.search(clause) and (_COMPUTED.search(clause) or "input" in clause.lower()):
                inputs = True
                yield _Site(line_no, ids, row, row, cited, True, text)
            elif re.search(r"[A-Za-z]{2}", clause):
                yield _Site(line_no, ids, [], [], cited, False, text)
            else:
                direct = True
                yield _Site(line_no, ids, row, row, cited, False, text)
    # computed figures: those of a labelled cell, and the whole row when its label or its only citations say so
    computed += [s.fig for s in row if s.fig.figure and (s.computed or (inputs and not direct))]


def _sites(report: str) -> tuple[list[_Site], list[Fig]]:
    """(every citation site, every figure the report labels as computed), in report order."""
    sites: list[_Site] = []
    computed: list[Fig] = []
    for no, line in enumerate(report.splitlines(), 1):
        if not (_ID.search(line) or _COMPUTED.search(line)):
            continue
        cells = _cells(line)
        if cells is not None:
            sites += _in_row(no, line, cells, computed)
        elif not line.strip().startswith("|"):  # the "3." of a numbered list and "- [ ]" are not numbers
            sites += _in_text(no, _LIST_MARK.sub(lambda m: " " * len(m.group(0)), line, count=1), computed)
    return sites, computed


# --------------------------------------------------------------------------- the check
# One outcome per citation of a claim that has a value (they sum to `PrintedReport.judged`):
#   value          its value is printed in the scope
#   statement      another figure of its own statement or period is printed (its value is not)
#   input          cited as an input of a computed figure
#   no_number      the scope prints no number at all
#   other_figures  the scope prints numbers, but none that could be held against it: other cited claims' figures,
#                  figures of another kind, labelled computations, or bare integers
#   mismatch       a Finding
OUTCOMES = ("value", "statement", "input", "no_number", "other_figures", "mismatch")


@dataclass(frozen=True)
class Finding:
    """The report prints a figure beside a citation that the cited claim does not hold."""

    claim_id: int
    kind: str  # value | sign | scale | percent
    importance: str | None
    held: str
    printed: str
    line: int
    text: str

    def message(self) -> str:
        why = {"sign": "with the opposite sign", "scale": "the same digits in another scale",
               "percent": "per cent and fraction confused"}.get(self.kind)  # fmt: skip
        return (f"[C{self.claim_id}] holds {self.held} but the report prints {self.printed} beside it"
                + (f" ({why})" if why else "") + f', line {self.line}: "{_clip(self.text, self.claim_id)}"')  # fmt: skip


def _clip(text: str, claim_id: int, width: int = 150) -> str:
    """The part of a scope that ends at the citation, for a message."""
    at = text.find(f"[C{claim_id}]")
    end = len(text) if at < 0 else at + len(f"[C{claim_id}]")
    return ("…" if end > width else "") + text[max(0, end - width) : end]


# words that say "lower" in a report without being a loss word of `verify.values` ("a share-count shrink of 2.32%")
_LOWER = re.compile(r"\b(?:shr[aiu]nk\w*|below|lower|less|drawdowns?|discount|reduc\w+|cuts?|trail\w*|worse|behind|"
                    r"under\w*|short\w*|minus|ero\w+|burn)\b", re.I)  # fmt: skip
# a figure the author sets as a condition, not one the report states: "at least 1.0x", "below ₹250", "≥ 15%"
_BOUND = re.compile(r"(?:\bat (?:least|most)|\b(?:below|above|under|over|exceeds?|reach(?:es)?|than|up to|minimum|"
                    r"maximum)(?: of)?|[<>≤≥])\s*(?:about|around|roughly|~)?\s*$", re.I)  # fmt: skip


def _printed_sign(s: _Seen) -> int:
    """-1 / +1 when the report prints the figure as negative / positive (a sign, an accounting bracket, or a
    profit / loss word in its own clause), else 0."""
    if s.fig.neg or s.fig.plus:
        return -1 if s.fig.neg else 1
    clause = re.split(r"[;:(|—–]", s.before)[-1]
    src = near_polarity(clause, s.after)
    return 0 if src > 0 and _LOWER.search(clause) else src


def _sign_clash(h: _Held, s: _Seen) -> bool:
    """Positive evidence that the figure is printed with the sign opposite to the claim's (as
    `verify.values.sign_conflict`, without its rule that an unsigned figure contradicts a negative claim)."""
    src = _printed_sign(s)
    claim = -1 if h.neg or h.word < 0 else h.word
    if claim < 0:
        return src > 0
    if claim > 0:
        return src < 0
    # a bare claim against a printed negative: only where a minus cannot be an outflow convention
    return s.fig.neg and (
        h.kind in ("pct", "frac") or bool(POS_RE.search(re.sub(r"\([^()]*\)", " ", s.label)))
    )


def _kind_of(h: _Held, f: Fig) -> str:
    """Why an unmatched figure looks like the claim's: its digits under another scale, or % against fraction."""
    assert h.value is not None
    bare = Fig(f.value, f.decimals, False, False, None, None, "plain", True, f.start, f.end, f.text)
    if f.scale is not None and (matches(bare, h.value, h.unit) or matches(bare, h.value, None)):
        return "scale"
    if f.kind == "pct" and h.kind in ("pct", "frac") and matches(bare, h.value, h.unit):
        return "percent"
    return "value"


@dataclass(frozen=True)
class TwoValues:
    """One metric and period printed with two values: two cited claims, both printed, more than CONFLICT_TOL apart."""

    metric: str
    period: str
    a: int
    b: int
    a_holds: str
    b_holds: str
    a_line: int
    b_line: int

    def message(self) -> str:
        return (f"{self.metric} ({self.period}) is printed with two values: {self.a_holds} [C{self.a}] on line "
                f"{self.a_line} and {self.b_holds} [C{self.b}] on line {self.b_line}")  # fmt: skip


@dataclass
class PrintedReport:
    findings: list[Finding] = field(default_factory=list)
    two_values: list[TwoValues] = field(default_factory=list)
    outcomes: dict[str, int] = field(default_factory=lambda: dict.fromkeys(OUTCOMES, 0))
    cites: list[tuple[int, int, str]] = field(default_factory=list)  # (claim id, line, outcome)

    @property
    def judged(self) -> int:
        return sum(self.outcomes.values())


def _stated(s: _Seen) -> bool:
    """Does the report state this figure itself? Not a labelled computation, not a figure in double quotation marks
    (quoted or discussed: 'any "₹20 crore" figure is wrong'), not a bound the author sets ("at least 1.0x")."""
    quoted = s.before.rstrip()[-1:] in ('"', "“") and s.after.lstrip()[:1] in ('"', "”")
    return not (s.computed or quoted or _BOUND.search(s.before))


def _judge(
    h: _Held, site: _Site, held: Mapping[int, _Held], computed: list[Fig]
) -> tuple[str, Finding | None]:
    """The outcome of one claim at one citation site. `computed`: the figures the report labels as computations
    anywhere; repeated later without the label they are still nobody's ledger value."""
    assert h.value is not None
    if site.is_input:
        return "input", None
    hit = [s for s in site.wide if matches(s.fig, h.value, h.unit)]
    if any(not _sign_clash(h, s) for s in hit):
        return "value", None
    if hit:
        return "mismatch", Finding(
            h.id, "sign", h.importance, h.holds(), hit[-1].fig.text, site.line, site.text
        )
    if any(_same(s.fig, o) for s in site.wide for o in h.own):
        return "statement", None
    if not site.wide:
        return "no_number", None
    others = [held[i] for i in site.cited if i != h.id and i in held]

    def explained(f: Fig) -> bool:
        return any(_same(f, x) for x in computed) or any(
            (o.value is not None and matches(f, o.value, o.unit)) or any(_same(f, x) for x in o.own)
            for o in others
        )

    accused = [s for s in site.near if _stated(s) and _fits(s.fig, h) and not explained(s.fig)]
    if not accused:
        return "other_figures", None
    f = accused[-1].fig  # the one nearest the citation
    return "mismatch", Finding(h.id, _kind_of(h, f), h.importance, h.holds(), f.text, site.line, site.text)


def _quantity(h: _Held) -> Decimal:
    """One form per quantity, as `verify.gate` compares claims: money in base units, a percentage as a fraction."""
    assert h.value is not None
    if h.kind == "money":
        return h.value * _unit(h.unit)[2]
    return h.value / 100 if h.kind == "pct" else h.value


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _blocks(report: str) -> dict[int, int]:
    """line number -> the number of its block: a run of lines with no blank line or heading between them (one
    table, one list, one paragraph)."""
    out: dict[int, int] = {}
    block = 0
    for no, line in enumerate(report.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            block += 1
        out[no] = block
    return out


def _two_values(
    held: Mapping[int, _Held], printed: Mapping[int, list[int]], block: Mapping[int, int]
) -> list[TwoValues]:
    """Pairs of printed claims with one metric and period and two values. The ledger's metric and period do not
    name the entity ("peer_pe", "max_drawdown" hold one claim per peer), so two claims the report itself sets side
    by side, in one table, list or paragraph, are a comparison and not a contradiction; nor are amounts in two
    currencies or kinds of unit."""
    groups: dict[tuple[str, str], list[_Held]] = {}
    for cid in sorted(printed):
        h = held[cid]
        if h.metric and h.period:
            groups.setdefault((_key(h.metric), _key(h.period)), []).append(h)
    out: list[TwoValues] = []
    for items in groups.values():
        for i, a in enumerate(items):
            for b in items[i + 1 :]:
                qa, qb = _quantity(a), _quantity(b)
                top = max(abs(qa), abs(qb))
                apart = bool(top) and abs(qa - qb) / top > CONFLICT_TOL
                beside = {block[n] for n in printed[a.id]} & {block[n] for n in printed[b.id]}
                if apart and not beside and (a.kind, a.cur) == (b.kind, b.cur):
                    out.append(TwoValues(a.metric, a.period, a.id, b.id, a.holds(), b.holds(), printed[a.id][0],
                                         printed[b.id][0]))  # fmt: skip
    return out


def check_printed(report_markdown: str, claims: Mapping[int, Any]) -> PrintedReport:
    """Judge every canonical citation of the report against the ledger rows in `claims` (id -> `Claim`, or any
    object with its attributes). Ids missing from `claims` are skipped: the gate reports those itself."""
    held = {i: _held(c) for i, c in claims.items()}
    rep = PrintedReport()
    printed: dict[int, list[int]] = {}  # claim -> the lines that print its value
    sites, computed = _sites(report_markdown)
    for site in sites:
        for cid in site.ids:
            h = held.get(cid)
            if h is None or h.value is None:
                continue
            outcome, finding = _judge(h, site, held, computed)
            rep.outcomes[outcome] += 1
            rep.cites.append((cid, site.line, outcome))
            if finding:
                rep.findings.append(finding)
            if outcome == "value":
                printed.setdefault(cid, []).append(site.line)
    rep.two_values = _two_values(held, printed, _blocks(report_markdown))
    return rep
