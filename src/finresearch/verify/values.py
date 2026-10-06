"""Value-in-source check for one numeric claim: is its figure printed at the cited lines with the same sign, in the
same unit and in the column of the same period? (issue #217; the gaps the verification audit of PR #160 left open)

`check_value` reads the cited lines (± the citation tolerance) of a pdftotext -layout document and returns one
`ValueCheck`. A number matches when its magnitude equals the claim's value after unit normalisation (relative
tolerance as `verify.gate.REL_TOL`), and then:

* sign      — explicit minus signs ("-", "−"; not the hyphen of "2025-26" or "10 - 12") and bracketed negatives
              "(1,234)" on the source side; on both sides, polarity words near the number (loss / decline / decrease /
              used in / paid vs profit / growth / increase / generated). "declined to ₹100 crore" is a level, not a
              negative. A row label carrying both words ("Profit / (Loss)", "from / (used in)") is neutral, so its
              brackets decide. Opposite signs -> `sign_mismatch`.
* unit      — the source unit, nearest first: next to the number ("₹ 1,050 crore", "12.5%"), in the row label
              ("Basic EPS (in ₹)", "Revenue growth (%)"), or the nearest declaration above ("(₹ in lakh)",
              "(In ₹ crore, except per share data)", "(in thousands)") within UNIT_LOOKBACK lines of the same page.
              Both sides are normalised to rupees (or dollars) before comparing; the claim's own digits under a
              different scale -> `unit_mismatch`. A table-level money scale never applies to per-share or % claims
              ("unless otherwise specified"). No unit found -> warning "unit unknown".
* period    — for a cell of a table row (≥ 2 whitespace-separated number cells), the period of its column: header
              phrases above the row (same page, within HEADER_LOOKBACK lines) are attached to the column they sit
              over (a phrase spanning several columns attaches to each), or the row label's own date ("March 31,
              2025  5.23  5.23"). A cell in a column of another period -> `period_mismatch` (only when the header
              gives ≥ 2 distinct column periods, so a stray date is never a header). A cell whose column period, or
              the claim's period, cannot be read -> warning "period unverified".
* basis     — the nearest statement title above on the same page ("Standalone Balance Sheet") against the claim's
              basis (`verify.identities.basis_of`): consolidated vs standalone -> `basis_mismatch`.

Statuses: pass (possibly with warnings) / not_found / sign_mismatch / unit_mismatch / period_mismatch /
basis_mismatch. A match anywhere in the window that passes wins over a mismatch elsewhere. How the gate acts on
them is documented in `verify.gate`.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

REL_TOL = Decimal("0.0005")  # same as verify.gate: printed figures are rounded to 2 dp
UNIT_LOOKBACK = 80  # lines above the cited line searched for a unit declaration (stops at a page break)
HEADER_LOOKBACK = 60  # lines above a table row searched for its period header (stops at a page break)
BASIS_LOOKBACK = 80
HARD = ("sign_mismatch", "unit_mismatch", "period_mismatch")
WARN_UNIT, WARN_PERIOD = "unit unknown", "period unverified"


# --------------------------------------------------------------------------- number tokens
@dataclass
class Tok:
    value: Decimal  # signed as printed
    start: int  # span of the digits (and brackets / minus) on the line
    end: int
    neg: bool  # printed negative: "-12.5", "−12.5", "(1,234)"


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_VALUE_LIKE = re.compile(r"\d\.\d|\d,\d")  # a figure, not a day ("31,") or a year


def tokens(line: str) -> list[Tok]:
    """Numbers on a line with their spans and printed sign. Digits glued to letters ("FY2026", "Q1") are skipped."""
    out: list[Tok] = []
    for m in _NUM.finditer(line):
        raw = m.group(0).rstrip(",")
        a, b = m.start(), m.start() + len(raw)
        glued = a and (line[a - 1].isalpha() or (line[a - 1] in "./" and a > 1 and line[a - 2].isdigit()))
        if glued or re.match(r"/\d", line[b : b + 2]):  # "FY2026", "Q1", "2.4.2", "16/10/2025"; not "₹25/-"
            continue
        try:
            v = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            continue
        before = line[:a]
        lead = before.rstrip(" ₹$`").rstrip()  # "( ₹ 1,234)" and "-₹120"
        neg = False
        if lead.endswith("(") and (len(lead) == 1 or not lead[-2].isalnum()):
            after = line[b:].lstrip(" %")
            if after.startswith(")"):
                neg, a, b = True, before.rfind("("), b + line[b:].index(")") + 1
        if not neg and lead and lead[-1] in "-−–":
            prev = lead[:-1]
            gap = len(prev) - len(prev.rstrip())
            # a minus, not a range / hyphen: "2025-26", "2%-3%" and "10 - 12" are ranges; a cell "937   -7.7%" is not
            if not prev.strip() or prev.rstrip()[-1] not in "0123456789%" or gap >= 2:
                neg, a = True, len(lead) - 1
        out.append(Tok(-v if neg else v, a, b, neg))
    return out


def has_value_like(line: str) -> bool:
    return any(_VALUE_LIKE.search(line[t.start : t.end]) for t in tokens(line))


# --------------------------------------------------------------------------- sign words
_NEG_W = (r"loss(?:es)?|declin\w*|decreas\w*|fell|fall(?:en|ing)?|drop(?:ped)?|negative|deficit|outflows?|"
          r"used in|paid(?!-up)|payments?|purchases?|repayments?|de-?growth|contraction|shrank|down")  # fmt: skip
_POS_W = r"profits?|growth|grew|increas\w*|rose|ris(?:e|ing)|gains?|surplus|inflows?|generated|up|improved"
NEG_RE = re.compile(rf"\b(?:{_NEG_W})\b", re.I)
POS_RE = re.compile(rf"\b(?:{_POS_W})\b", re.I)
_LEVEL_RE = re.compile(r"\b(?:to|from|at)\b", re.I)  # "declined to ₹100 crore": the level, not the change


def polarity(text: str) -> int:
    """-1 / +1 when the text carries only negative / only positive words, else 0 (none, or both)."""
    n, p = bool(NEG_RE.search(text)), bool(POS_RE.search(text))
    return -1 if n and not p else (1 if p and not n else 0)


def near_polarity(before: str, after: str = "") -> int:
    """Polarity of the words just before a number (and just after it, up to punctuation), ignoring a polarity word
    followed by "to / from / at" (a level)."""
    words = re.findall(r"[\w'-]+|[;,.]", before)[-6:]
    cut = max((i for i, w in enumerate(words) if w in ";,."), default=-1)
    seg = words[cut + 1 :]
    last = max((i for i, w in enumerate(seg) if NEG_RE.fullmatch(w) or POS_RE.fullmatch(w)), default=-1)
    if last >= 0 and any(_LEVEL_RE.fullmatch(w) for w in seg[last + 1 :]):
        seg = seg[:last]
    tail = re.split(r"[;,.()]|\d", after, maxsplit=1)[0].split()[:3]
    return polarity(" ".join(seg + tail))


_BRACKETED = re.compile(r"\(([^()]*)\)")


def row_sign(label: str, printed_neg: bool) -> int:
    """The sign a table row gives its figure. A bracketed word in the label says what a bracketed figure means:
    "Profit / (Loss)" (1,234) is a loss, "(Gain)/loss on foreign currency" (32.30) a gain, "from / (used in)" (500)
    an outflow; an unbracketed figure then takes the other word."""
    bracketed = polarity(" ".join(_BRACKETED.findall(label)))
    if printed_neg:
        return bracketed or -1
    return polarity(_BRACKETED.sub(" ", label)) if bracketed else polarity(label)


def sign_conflict(claim_neg: bool, claim_word: int, src: int, printed_neg: bool, label: str) -> bool:
    """Opposite signs. `src` is the source's sign (-1 / 0 / +1, 0 = no evidence: an unsigned figure in a plain row);
    a source with no evidence only conflicts with an explicitly negative claimed value."""
    c = -1 if claim_neg or claim_word < 0 else claim_word
    if c < 0:
        return src > 0 or (claim_neg and src == 0)
    if c > 0:
        return src < 0
    # a bare claim against a bracketed figure whose row says the unbracketed reading is positive ("Profit / (Loss)",
    # "Net cash generated from / (used in)"); outflow rows ("Income taxes paid (8,648)") carry no such word
    return printed_neg and src < 0 and bool(POS_RE.search(_BRACKETED.sub(" ", label)))


# --------------------------------------------------------------------------- units
@dataclass(frozen=True)
class Unit:
    kind: str  # money | pct | per_share
    cur: str | None = None  # INR | USD | None (scale given without a currency: "(in thousands)")
    scale: Decimal = Decimal(1)
    level: str = "table"  # adjacent | row | table

    def label(self) -> str:
        if self.kind != "money":
            return {"pct": "%", "per_share": f"{self.cur or 'INR'} per share"}[self.kind]
        name = {10**7: "crore", 10**6: "million", 10**5: "lakh", 10**9: "billion", 1000: "thousand", 1: ""}
        return f"{self.cur or ''} {name.get(int(self.scale), str(self.scale))}".strip() or "money"


_SCALES = (("crore", 10**7), ("cr", 10**7), ("lakh", 10**5), ("lac", 10**5), ("million", 10**6), ("mn", 10**6),
           ("billion", 10**9), ("bn", 10**9), ("thousand", 1000), ("'000", 1000))  # fmt: skip
_SC = r"(crores?|cr\b\.?|lakhs?|lacs?|millions?|mn\b|billions?|bn\b|thousands?|'000)"
_CUR = r"(₹|rs\b\.?|inr|rupees|us\$|usd|\$|`)"  # "`" is how some annual-report fonts extract the ₹ glyph
_DECLS = [re.compile(p, re.I) for p in (
    rf"\(\s*(?:all\s+)?(?:amounts?|figures|amt\.?)?\s*(?:are\s+)?(?:in\s+)?{_CUR}?\s*(?:in\s+)?{_SC}",
    rf"(?:^|\s)(?:all\s+)?(?:amounts?|figures)\s+(?:are\s+)?in\s+{_CUR}?\s*{_SC}",
    rf"(?:^|[\s(]){_CUR}\s*in\s+{_SC}",
    rf"(?:^|[\s(])in\s+{_CUR}\s*{_SC}",
)]  # fmt: skip
_RUPEE_ONLY = re.compile(
    rf"\(\s*(?:in\s+)?{_CUR}\s*(?:\)|,|except|unless)", re.I
)  # "(In ₹ except share data)"
_PCT_DECL = re.compile(r"\(\s*(?:in\s+)?(?:%|per\s?cent|percent)\s*\)|%|\bper\s?cent\b|\bpercent", re.I)
_PER_SHARE = re.compile(r"per\s+(?:equity\s+)?share|\beps\b", re.I)


def _cur(tok: str | None) -> str | None:
    if not tok:
        return None
    t = tok.lower()
    return "USD" if ("$" in t or "usd" in t) else "INR"


def _scale(tok: str) -> Decimal:
    t = tok.lower().rstrip(".")
    return Decimal(next(s for k, s in _SCALES if t.startswith(k)))


def declared_unit(text: str, level: str = "table", *, per_share_words: bool = True) -> Unit | None:
    """A unit declaration in a table / page header or a row label. `per_share_words`: "per share" in a row label
    makes it a per-share row (in prose it belongs to another figure: "Buyback at ₹1,800 per share  18,000")."""
    for rx in _DECLS:
        m = rx.search(text)
        if m:
            return Unit("money", _cur(m.group(1)), _scale(m.group(2)), level)
    if level == "row":
        if _PCT_DECL.search(text):
            return Unit("pct", level=level)
        m = _RUPEE_ONLY.search(text)
        if m or (per_share_words and _PER_SHARE.search(text)):
            return Unit("per_share", _cur(m.group(1)) if m else None, Decimal(1), level)
    elif _RUPEE_ONLY.search(text) and (re.search(r"except|unless", text, re.I) or len(text.split()) <= 4):
        # "(In ₹ except share data)"; not a row label such as "Basic earnings per share (in ₹)"
        return Unit("money", _cur(_RUPEE_ONLY.search(text).group(1)), Decimal(1), level)
    elif re.search(r"\(\s*(?:in\s+)?(?:%|per\s?cent)\s*\)", text, re.I):
        return Unit("pct", level=level)
    return None


_ADJ_AFTER = re.compile(rf"^\s?(?:(%|per\s?cent\b|percent\b)|{_SC})", re.I)
_ADJ_BEFORE = re.compile(rf"{_CUR}\s?$", re.I)


def adjacent_unit(line: str, t: Tok, next_line: str = "") -> Unit | None:
    after, before = line[t.end :], line[: t.start]
    if not after.strip():
        after = " " + next_line.lstrip()  # "`1,326" at the end of a line, "crore" wrapped onto the next
    m = _ADJ_AFTER.match(after)
    cur = _ADJ_BEFORE.search(before)
    if m and m.group(1):
        return Unit("pct", level="adjacent")
    if m:
        return Unit("money", _cur(cur.group(1)) if cur else None, _scale(m.group(2)), "adjacent")
    if cur:
        per = _PER_SHARE.match(after.strip()) is not None
        return Unit("per_share" if per else "money", _cur(cur.group(1)), Decimal(1), "adjacent")
    return None


# --------------------------------------------------------------------------- periods
# a period is (end year, end month or None, length in months or None); FY2026 = (2026, 3, 12), Q1FY2027 =
# (2026, 6, 3), "As at March 31, 2025" = (2025, 3, None), a bare header year "2026" = (2026, None, None)
Period = tuple[int, int | None, int | None]
_MON = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}  # fmt: skip
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_Y = r"((?:19|20)\d{2}|'?\d{2})"
_FY = r"(?:fy|f\.y\.|fiscal(?:\s+year)?|financial\s+year)\s?'?((?:19|20)\d{2}|\d{2})(?:\s?[-–/]\s?(\d{2,4}))?"
_REJECT = re.compile(r"\bttm\b|\bltm\b|forward|estimate|guidance|pro ?forma|\bvs\.?\b|\bcagr\b|\bfy\s?\d{2,4}e\b"
                     r"|\bto\s+(?:fy|q[1-4]|20\d\d)", re.I)  # fmt: skip
_PERIOD_WORD = re.compile(rf"{_FY}|\b{_MONTH}\b|\b(?:19|20)\d{{2}}\b(?![.,]\d)|\bq[1-4]\b|\bh[12]\b|\b9m\b|year ended"
                          r"|quarter|months|period ended|as at|as of", re.I)  # fmt: skip
_DUR = ((re.compile(r"three months|3 months|quarter", re.I), 3), (re.compile(r"six months|6 months|half[- ]year", re.I), 6),
        (re.compile(r"nine months|9 months", re.I), 9),
        (re.compile(r"twelve months|12 months|\byear ended|\bfor the year", re.I), 12))  # fmt: skip


def _yr(tok: str) -> int:
    y = int(tok.lstrip("'"))
    return y + 2000 if y < 100 else y


def _fy_end(m: re.Match) -> int:
    """FY2026, FY26, FY2025-26, Fiscal 2026, F.Y. 2025-26 -> the March the fiscal year ends in."""
    first, second = m.group(1), m.group(2)
    if second and len(first) == 4 and int(second[-2:]) == (int(first) + 1) % 100:
        return _yr(first) + 1
    return _yr(first)


def period_of(text: str | None, *, bare_year: bool = False) -> Period | None:
    """The one period a text names, or None when it names none, several, a range, TTM or an estimate."""
    t = (text or "").strip()
    if not t or _REJECT.search(t):
        return None
    found: list[Period] = []
    for m in re.finditer(rf"\b([qh][1-4]|9m)\s?{_FY}", t, re.I):
        k, fy = m.group(1).lower(), _fy_end(re.match(_FY, m.group(0)[len(m.group(1)) :].strip(), re.I))
        found.append({"q1": (fy - 1, 6, 3), "q2": (fy - 1, 9, 3), "q3": (fy - 1, 12, 3), "q4": (fy, 3, 3),
                      "h1": (fy - 1, 9, 6), "h2": (fy, 3, 6), "9m": (fy - 1, 12, 9)}.get(k, (fy, 3, 12)))  # fmt: skip
    rest = re.sub(rf"\b(?:[qh][1-4]|9m)\s?{_FY}", " ", t, flags=re.I)
    found += [(_fy_end(m), 3, 12) for m in re.finditer(rf"\b{_FY}\b", rest, re.I)]
    rest = re.sub(rf"\b{_FY}\b", " ", rest, flags=re.I)
    dates = [(int(m.group(1)), int(m.group(2))) for m in re.finditer(r"\b(20\d{2})-(\d{2})-\d{2}\b", rest)]
    rest = re.sub(r"\b20\d{2}-\d{2}-\d{2}\b", " ", rest)
    for m in re.finditer(
        rf"\b(?:\d{{1,2}}(?:st|nd|rd|th)?[-\s]+)?{_MONTH}[-\s]*(?:\d{{1,2}},?\s*)?[-\s']*{_Y}\b", rest, re.I
    ):
        dates.append((_yr(m.group(2)), _MON[m.group(1).lower()[:3]]))
    rest = re.sub(rf"\b{_MONTH}[-\s]*(?:\d{{1,2}},?\s*)?[-\s']*{_Y}\b", " ", rest, flags=re.I)
    dur = next((n for rx, n in _DUR if rx.search(t)), None)
    found += [(y, mo, dur) for y, mo in dates]
    if bare_year and not found:
        found += [(int(y), None, dur) for y in re.findall(r"\b((?:19|20)\d{2})\b(?![.,]\d)", rest)]
    if not found:
        return None
    y, mo, n = found[0]
    for y2, mo2, n2 in found[1:]:
        if y2 != y or (mo and mo2 and mo != mo2) or (n and n2 and n != n2):
            return None  # two different periods
        mo, n = mo or mo2, n or n2
    return (y, mo, n)


def same_period(a: Period, b: Period) -> bool:
    return a[0] == b[0] and (a[1] is None or b[1] is None or a[1] == b[1]) and (
        a[2] is None or b[2] is None or a[2] == b[2])  # fmt: skip


def period_label(p: Period) -> str:
    y, mo, n = p
    if mo == 3 and n == 12:
        return f"FY{y}"
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(mo or 0)
    if n == 3 and q:
        return f"Q{q}FY{y + (1 if q < 4 else 0)}"
    end = f"{y}-{mo:02d}" if mo else str(y)
    return f"{n}M to {end}" if n else end


# --------------------------------------------------------------------------- table layout
def cells(line: str) -> list[Tok]:
    """The number cells of a table row: the numbers after the row label when ≥ 2 of them are separated by
    whitespace only (prose has words between its numbers). [] for prose and single figures."""
    toks = tokens(line)
    first = next((i for i, t in enumerate(toks) if _VALUE_LIKE.search(line[t.start : t.end])), 0)
    toks = toks[first:]
    if len(toks) < 2:
        return []
    gaps = [line[a.end : b.start] for a, b in itertools.pairwise(toks)]
    return toks if all(re.fullmatch(r"[\s%₹$`|\-–]*", g) and g.count(" ") >= 2 for g in gaps) else []


def row_label(line: str, row: list[Tok]) -> str:
    return line[: row[0].start] if row else line


def _is_page_break(line: str) -> bool:
    return "\f" in line


def _header_lines(lines: list[str], idx: int) -> list[str]:
    """The header block of the table whose row is lines[idx] (0-based): the nearest run of consecutive non-blank
    lines above it that names periods and holds no figures, on the same page."""
    i, block = idx - 1, []
    if _is_page_break(lines[idx]):
        return []
    while i >= 0 and idx - i <= HEADER_LOOKBACK:
        ln = lines[i]
        header_like = ln.strip() and _PERIOD_WORD.search(ln) and not has_value_like(ln)
        if header_like:
            j = i
            while j >= 0 and i - j < 8 and lines[j].strip() and not has_value_like(lines[j]):
                block.insert(0, lines[j])
                if _is_page_break(lines[j]):
                    break
                j -= 1
            k = i + 1  # non-figure lines between the header and the first row ("YoY  QoQ")
            while k < idx and lines[k].strip() and not has_value_like(lines[k]) and k - i < 4:
                block.append(lines[k])
                k += 1
            return block
        if _is_page_break(ln):
            return []
        i -= 1
    return []


def column_headers(lines: list[str], idx: int, row: list[Tok]) -> list[str]:
    """The header text over each cell of a table row. A phrase (words separated by single spaces) belongs to every
    cell whose centre it spans, else to the nearest cell if it sits over it."""
    centres = [(t.start + t.end) / 2 for t in row]
    left = row[0].start - 2
    texts: list[list[str]] = [[] for _ in row]
    for ln in _header_lines(lines, idx):
        for m in re.finditer(r"\S+(?: \S+)*", ln.replace("\f", " ")):
            a, b = m.start(), m.end()
            if b <= left:
                continue  # the label column ("Particulars")
            spanned = [i for i, c in enumerate(centres) if a - 1 <= c <= b + 1]
            if not spanned:  # the nearest cell, if the phrase sits over it (not over an empty "-" column)
                near = min(range(len(row)), key=lambda i: abs(centres[i] - (a + b) / 2))
                spanned = [near] if abs(centres[near] - (a + b) / 2) <= 8 + (b - a) / 2 else []
            for i in spanned:
                texts[i].append(m.group(0))
    return [" ".join(words) for words in texts]


def column_period(header: str) -> Period | None:
    if re.search(r"%|growth|\byoy\b|\bqoq\b|change|variance|\bnote\b", header, re.I):
        return None  # a growth / share / note column, not a period's level
    return period_of(header, bare_year=True)


def _above(lines: list[str], idx: int, n: int):
    """lines[idx], lines[idx-1], ... up to n lines, stopping after the first line of the page (it holds the \\f)."""
    for i in range(idx, max(-1, idx - n), -1):
        yield i, lines[i]
        if _is_page_break(lines[i]):
            return


def table_unit(lines: list[str], idx: int) -> Unit | None:
    for _, ln in _above(lines, idx, UNIT_LOOKBACK):
        u = declared_unit(ln)
        if u:
            return u
    return None


_BASIS_TITLE = re.compile(
    r"statement|balance sheet|profit and loss|cash flows?|financial|results|information", re.I
)


def page_basis(lines: list[str], idx: int) -> str | None:
    """standalone / consolidated from the nearest statement title above (a line naming exactly one of them)."""
    for _, ln in _above(lines, idx, BASIS_LOOKBACK):
        low = ln.lower()
        s, c = "standalone" in low, "consolidated" in low
        if (s or c) and len(ln.split()) <= 14 and _BASIS_TITLE.search(ln):
            return None if s and c else ("standalone" if s else "consolidated")
    return None


def unit_of(lines: list[str], idx: int, t: Tok, row: list[Tok]) -> Unit | None:
    line = lines[idx]
    adj = adjacent_unit(line, t, lines[idx + 1] if idx + 1 < len(lines) else "")
    if adj and adj.kind == "money" and adj.scale == 1:
        # "₹ 1,326" with no scale word: the scale of the page's declaration if there is one, else rupees
        tab = table_unit(lines, idx)
        if tab and tab.kind == "money":
            return Unit("money", adj.cur or tab.cur, tab.scale, "table")
    if adj:
        return adj
    if row:
        label = row_label(line, row)
        col = declared_unit(column_headers(lines, idx, row)[row.index(t)], "row") if t in row else None
        if col and not declared_unit(label, "row"):
            # "Basic EPS (in ₹)" or "% of revenue" over the column; a ₹ scale over it is as weak as a table's
            # (it often spans the whole header: "(in ₹ million, unless otherwise specified)")
            return (
                Unit(col.kind, col.cur, col.scale, "table") if col.kind == "money" and col.scale != 1 else col
            )
    else:  # prose: the words since the previous number ("100.00% 1,65,59,99,376": the % is the previous one's)
        prev = max((x.end for x in tokens(line) if x.end <= t.start), default=0)
        label = line[prev : t.start].lstrip(" %")
    return declared_unit(label, "row", per_share_words=bool(row)) or table_unit(lines, idx)


# --------------------------------------------------------------------------- the check
@dataclass
class ValueCheck:
    status: str = "not_found"
    warnings: list[str] = field(default_factory=list)
    detail: str = ""
    source_unit: str | None = None
    source_period: str | None = None
    line: int | None = None

    @property
    def found(self) -> bool:
        return self.status == "pass"

    def rank(self) -> tuple[int, int]:
        order = ["pass", "basis_mismatch", "period_mismatch", "unit_mismatch", "sign_mismatch", "not_found"]
        return order.index(self.status), len(self.warnings)

    def to_checks(self) -> dict[str, Any]:
        out: dict[str, Any] = {"value_check": self.status}
        if self.warnings:
            out["value_warnings"] = list(self.warnings)
        if self.detail:
            out["value_detail"] = self.detail
        if self.source_unit:
            out["source_unit"] = self.source_unit
        if self.source_period:
            out["source_period"] = self.source_period
        return out


def _close(a: Decimal, b: Decimal) -> bool:
    if a == b:
        return True
    d = max(abs(a), abs(b))
    return d != 0 and abs(a - b) / d <= REL_TOL


@dataclass
class _Claim:
    mag: Decimal
    kind: str  # money | pct | per_share | other (verify.identities.unit_kind)
    cur: str | None
    factor: Decimal
    neg: bool  # the value (or its figure in the statement) is negative
    word: int  # polarity of the words next to the figure in the statement, else of the metric name
    period: Period | None
    basis: str  # consolidated | standalone | unknown
    forms: list[Decimal]


def _claim(value: Decimal, unit: str | None, period: str | None, statement: str, metric: str) -> _Claim:
    from finresearch.verify.gate import candidate_forms
    from finresearch.verify.identities import basis_of, unit_kind

    kind, cur, factor = unit_kind(unit)
    if kind not in ("money", "pct", "per_share"):
        kind = "other"
    mag = abs(value)
    forms = [abs(f) for f in candidate_forms(mag, unit)]
    neg, word = value < 0, 0
    st = tokens(statement or "")
    for i, t in enumerate(st):
        if any(_close(abs(t.value), f) for f in forms):
            prev = st[i - 1].end if i else 0
            neg = neg or t.neg
            word = near_polarity(statement[prev : t.start], statement[t.end :])
            break
    word = word or polarity((metric or "").replace("_", " "))
    basis = basis_of({"metric": metric, "period": period, "statement": statement})
    return _Claim(mag, kind, cur, factor, neg, word, period_of(period), basis, forms)


def _unit_verdict(c: _Claim, u: Unit | None, v: Decimal) -> str:
    """ok | unknown | mismatch for a source number v (already a loose match of the claim's value)."""
    if c.kind == "other":
        return "ok"
    explicit = u is not None and u.level in ("adjacent", "row")
    if u is None or (
        not explicit and u.kind == "money" and (c.kind != "money" or c.factor == 1) and u.scale != 1
    ):
        # a table-level ₹ scale does not apply to per-share or % rows ("except per share data"), nor to a bare "INR"
        # claim, which is usually a per-share figure (dividend, price)
        return "unknown"
    if u.cur and c.cur and u.cur != c.cur:
        return "mismatch"
    if c.kind == "money":
        if u.kind == "pct":
            return "mismatch" if explicit else "unknown"
        return "ok" if _close(c.mag * c.factor, v * u.scale) else "mismatch"
    if c.kind == "pct":
        if u.kind != "pct":
            return "mismatch"
        return "ok" if _close(c.mag * c.factor, v / 100) else "mismatch"
    # per share
    if u.kind == "pct" or (u.kind == "money" and u.scale != 1):
        return "mismatch"
    return "ok" if _close(c.mag, v) else "mismatch"


def _src_label(lines: list[str], idx: int, t: Tok, row: list[Tok]) -> tuple[str, int]:
    """(the words that say what the number is, their polarity)."""
    line = lines[idx]
    if row:
        label = row_label(line, row)
        if not label.strip() and idx and not has_value_like(lines[idx - 1]):
            label = lines[idx - 1]  # "Net profit" on one line, its figures on the next
        return label, polarity(label)
    toks = tokens(line)
    prev = max((x.end for x in toks if x.end <= t.start), default=0)
    before = line[prev : t.start]
    if not prev and idx:
        before = lines[idx - 1][-60:] + " " + before  # a sentence wrapped onto this line
    return before, near_polarity(before, line[t.end :])


def _judge(lines: list[str], idx: int, t: Tok, row: list[Tok], c: _Claim) -> ValueCheck:
    v = abs(t.value)
    u = unit_of(lines, idx, t, row)
    res = ValueCheck(status="pass", line=idx + 1, source_unit=u.label() if u else None)
    unit = _unit_verdict(c, u, v)
    if unit == "mismatch":
        res.status, res.detail = "unit_mismatch", f"the cited figure {v} is in {u.label()}"  # type: ignore[union-attr]
        return res
    if unit == "unknown":
        res.warnings.append(WARN_UNIT)
    label, word = _src_label(lines, idx, t, row)
    src = row_sign(label, t.neg) if row else (-1 if t.neg else word)
    if sign_conflict(c.neg, c.word, src, t.neg, label):
        shown = f"({v})" if t.neg else str(v)
        res.status = "sign_mismatch"
        res.detail = f"the source prints {shown} next to '{' '.join(label.split())[-60:]}': the opposite sign"
        return res
    if row and t in row:
        own = period_of(row_label(lines[idx], row))
        if own:
            col: Period | None = own
        else:
            cps = [column_period(h) for h in column_headers(lines, idx, row)]
            col = cps[row.index(t)] if len({p for p in cps if p}) >= 2 else None
        if col:
            res.source_period = period_label(col)
        if c.period is None or col is None:
            res.warnings.append(WARN_PERIOD)
        elif not same_period(c.period, col):
            res.status = "period_mismatch"
            res.detail = f"{v} sits in the {period_label(col)} column, not {period_label(c.period)}"
            return res
    if c.basis != "unknown":
        b = page_basis(lines, idx)
        if b and b != c.basis:
            res.status, res.detail = "basis_mismatch", f"the cited page is {b}, the claim {c.basis}"
    return res


def check_value(lines: list[str], line_start: int, line_end: int | None, value: Decimal, unit: str | None,
                period: str | None, *, statement: str = "", metric: str = "", tolerance: int = 2) -> ValueCheck:  # fmt: skip
    """Check a claimed value against lines[line_start-tolerance .. line_end+tolerance] (1-based) of a document."""
    c = _claim(Decimal(value), unit, period, statement, metric)
    a = max(1, line_start - tolerance)
    b = min(len(lines), (line_end or line_start) + tolerance)
    best = ValueCheck()
    for idx in range(a - 1, b):
        line = lines[idx]
        row = cells(line)
        for t in tokens(line):
            if not any(_close(abs(t.value), f) for f in c.forms):
                continue
            res = _judge(lines, idx, t, row, c)
            if res.rank() < best.rank():
                best = res
    return best


def check_text(text: str, value: Decimal, unit: str | None, period: str | None, **kw: Any) -> ValueCheck:
    """The same check on a quoted text (a web citation's quote, or a citation without document lines)."""
    lines = text.split("\n")
    return check_value(lines, 1, len(lines), value, unit, period, tolerance=0, **kw)
