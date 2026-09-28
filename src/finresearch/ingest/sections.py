"""Map an Indian offer document (RHP / DRHP / Prospectus) to its SEBI ICDR sections with line & page ranges.

Offer documents follow the ICDR Schedule VI layout, so section headings are predictable. We find each heading
as a stand-alone (usually centred, upper-case) line *after* the table of contents, keep them in document order,
and each section runs until the next mapped heading. Headings are matched on normalised text (quotes, dashes,
spacing) and TOC lines (dotted leaders + page numbers) are ignored.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# canonical id -> heading patterns (normalised, upper-case). Order = typical document order.
ICDR_SECTIONS: list[tuple[str, list[str]]] = [
    ("DEFINITIONS", [r"DEFINITIONS AND ABBREVIATIONS"]),
    ("OFFER_DOCUMENT_SUMMARY", [r"(SUMMARY OF THE OFFER DOCUMENT|OFFER DOCUMENT SUMMARY)"]),
    ("RISK_FACTORS", [r"(SECTION [IVX]+\s*[-:]?\s*)?RISK FACTORS"]),
    ("THE_OFFER", [r"THE OFFER"]),
    (
        "SUMMARY_FINANCIAL_INFO",
        [r"SUMMARY (OF )?(RESTATED )?(CONSOLIDATED )?FINANCIAL (INFORMATION|STATEMENTS)"],
    ),
    ("GENERAL_INFORMATION", [r"GENERAL INFORMATION"]),
    ("CAPITAL_STRUCTURE", [r"CAPITAL STRUCTURE"]),
    ("OBJECTS_OF_THE_OFFER", [r"OBJECTS? OF THE (OFFER|ISSUE)"]),
    ("BASIS_FOR_OFFER_PRICE", [r"BASIS FOR (THE )?(OFFER|ISSUE) PRICE"]),
    ("TAX_BENEFITS", [r"STATEMENT OF (POSSIBLE )?SPECIAL TAX BENEFITS"]),
    ("INDUSTRY_OVERVIEW", [r"INDUSTRY OVERVIEW"]),
    ("OUR_BUSINESS", [r"OUR BUSINESS"]),
    ("KEY_REGULATIONS", [r"KEY (INDUSTRY )?REGULATIONS AND POLICIES( IN INDIA)?"]),
    ("HISTORY_CORPORATE", [r"HISTORY AND CERTAIN CORPORATE MATTERS"]),
    ("OUR_MANAGEMENT", [r"OUR MANAGEMENT"]),
    ("PROMOTERS", [r"OUR PROMOTERS? AND PROMOTER GROUP"]),
    ("GROUP_COMPANIES", [r"(OUR )?GROUP COMPANIES"]),
    ("DIVIDEND_POLICY", [r"DIVIDEND POLICY"]),
    (
        "RESTATED_FINANCIALS",
        [
            r"(SECTION [IVX]+\s*[-:]?\s*FINANCIAL INFORMATION|RESTATED (CONSOLIDATED )?(AND STANDALONE )?FINANCIAL (INFORMATION|STATEMENTS))"
        ],
    ),
    ("OTHER_FINANCIAL_INFO", [r"OTHER FINANCIAL INFORMATION"]),
    (
        "MDNA",
        [r"MANAGEMENT.?S DISCUSSION AND ANALYSIS OF FINANCIAL( CONDITION AND RESULTS OF( OPERATIONS)?)?"],
    ),
    ("CAPITALISATION", [r"CAPITALI[SZ]ATION STATEMENT"]),
    ("FINANCIAL_INDEBTEDNESS", [r"FINANCIAL INDEBTEDNESS"]),
    ("LITIGATION", [r"OUTSTANDING LITIGATION AND (OTHER )?MATERIAL DEVELOPMENTS"]),
    ("GOVT_APPROVALS", [r"GOVERNMENT AND OTHER APPROVALS"]),
    ("OTHER_REGULATORY_DISCLOSURES", [r"OTHER REGULATORY AND STATUTORY DISCLOSURES"]),
    ("TERMS_OF_OFFER", [r"TERMS OF THE (OFFER|ISSUE)"]),
    ("OFFER_STRUCTURE", [r"(OFFER|ISSUE) STRUCTURE"]),
    ("OFFER_PROCEDURE", [r"(OFFER|ISSUE) PROCEDURE"]),
    (
        "ARTICLES",
        [
            r"(MAIN PROVISIONS OF )?(THE )?ARTICLES OF ASSOCIATION|DESCRIPTION OF EQUITY SHARES AND TERMS OF THE ARTICLES"
        ],
    ),
    ("MATERIAL_CONTRACTS", [r"MATERIAL CONTRACTS AND DOCUMENTS FOR INSPECTION"]),
    ("DECLARATION", [r"DECLARATION"]),
]

_TOC_LINE = re.compile(r"\.{5,}|…{3,}|\s\d{1,3}\s*$")


@dataclass
class SectionSpan:
    canonical: str
    heading: str
    line_start: int  # 1-based, inclusive
    line_end: int  # 1-based, inclusive


def _norm(line: str) -> str:
    s = unicodedata.normalize("NFKC", line).replace("\f", "")
    s = s.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s).strip().upper()
    return s


def _compiled() -> list[tuple[str, re.Pattern]]:
    return [(cid, re.compile(rf"^(?:{'|'.join(pats)})$")) for cid, pats in ICDR_SECTIONS]


def find_toc_end(lines: list[str], search_limit: int = 3000) -> int:
    """Index (0-based) just after the last TOC-looking line near the start of the document."""
    last = 0
    for i, ln in enumerate(lines[:search_limit]):
        if re.search(r"\.{5,}\s*\d{1,3}\s*$", ln):
            last = i
    return last + 1


PRE_RISK = {"DEFINITIONS", "OFFER_DOCUMENT_SUMMARY"}  # the only sections printed before RISK FACTORS


def map_sections(lines: list[str]) -> list[SectionSpan]:
    """lines: from ingest.text.read_lines (split on '\\n' only).

    Two passes: locate RISK FACTORS first; every other section except the pre-risk ones must start after it.
    (The Offer Document Summary repeats sub-headings such as "OBJECTS OF THE OFFER".)
    """
    first = _scan(lines, find_toc_end(lines), only={"RISK_FACTORS"})
    risk_idx = first[0][0] if first else None
    hits = _scan(lines, find_toc_end(lines), only=PRE_RISK | {"RISK_FACTORS"})
    hits = [h for h in hits if h[1] == "RISK_FACTORS" or risk_idx is None or h[0] < risk_idx]
    rest_start = (risk_idx + 1) if risk_idx is not None else find_toc_end(lines)
    hits += _scan(lines, rest_start, exclude=PRE_RISK | {"RISK_FACTORS"})
    hits.sort()
    spans: list[SectionSpan] = []
    for k, (i, cid, heading) in enumerate(hits):
        end = hits[k + 1][0] - 1 if k + 1 < len(hits) else len(lines) - 1
        spans.append(SectionSpan(cid, heading, i + 1, end + 1))
    return spans


def _scan(lines: list[str], start: int, only: set[str] | None = None, exclude: set[str] | None = None):
    pats = [
        (c, rx) for c, rx in _compiled() if (only is None or c in only) and not (exclude and c in exclude)
    ]
    hits: list[tuple[int, str, str]] = []  # (idx, canonical, heading)
    seen: set[str] = set()
    for i in range(start, len(lines)):
        raw = lines[i]
        n = _norm(raw)
        if not n or len(n) > 110 or (_TOC_LINE.search(raw.replace("\f", "")) and "...." in raw):
            continue
        # heading lines are standalone: little else on the line, mostly upper-case letters
        letters = [c for c in n if c.isalpha()]
        if not letters or sum(c.isupper() for c in letters) / len(letters) < 0.9:
            continue
        for cid, rx in pats:
            if cid in seen:
                continue
            if rx.match(n):
                # guard against a running header / cross-reference repeating the title mid-section:
                # headings are preceded by a blank line or page break
                prev = lines[i - 1] if i else ""
                if prev.strip() and "\f" not in raw and "\f" not in prev:
                    continue
                hits.append((i, cid, raw.strip().replace("\f", "")))
                seen.add(cid)
                break
    return hits


def page_for_line(line_no: int, page_spans: list[tuple[int, int, int]]) -> int | None:
    """page_spans: [(page_no, line_start, line_end)] sorted."""
    lo, hi = 0, len(page_spans) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        p, a, b = page_spans[mid]
        if line_no < a:
            hi = mid - 1
        elif line_no > b:
            lo = mid + 1
        else:
            return p
    return None
