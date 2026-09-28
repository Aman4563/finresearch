"""Rebuild clean tables from `pdftotext -layout` text using character positions.

Indian offer documents print financial tables with wrapped multi-line column headers
("For the three / months period / ended June 30, / 2026"). Small local models (and big ones, at a
token cost) struggle to align those headers with number columns. This module does the alignment
deterministically, so LLMs only have to interpret labels, never parse layout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# numbers like 4,891.56  (84.17)  -  1,17,64,705  0.94%  ₹ 34
_NUM = re.compile(r"(?<![\w.])\(?-?[₹$]?\s?\d[\d,]*(?:\.\d+)?\)?%?(?![\w])|(?<=\s)-(?=\s|$)")


@dataclass
class Table:
    headers: list[str]  # one per value column (best-effort reconstruction of the printed header)
    rows: list[tuple[str, list[str]]] = field(default_factory=list)  # (label, values)
    unit_note: str | None = None
    periods: list[str | None] = field(
        default_factory=list
    )  # e.g. "3M ended 2026-06-30", "FY ended 2026-03-31"

    def column_labels(self) -> list[str]:
        return [
            p or h or f"col{i + 1}"
            for i, (h, p) in enumerate(
                zip(self.headers, self.periods or [None] * len(self.headers), strict=False)
            )
        ]

    def to_markdown(self) -> str:
        head = "| Particulars | " + " | ".join(self.column_labels()) + " |"
        sep = "|---" * (len(self.headers) + 1) + "|"
        body = ["| " + label + " | " + " | ".join(vals) + " |" for label, vals in self.rows]
        note = [f"_Units: {self.unit_note}_", ""] if self.unit_note else []
        return "\n".join([*note, head, sep, *body])


_YEAR = re.compile(r"(?:19|20)\d{2}")


def _is_value_token(tok: str) -> bool:
    """A table value, not a header fragment like '31,' or '2026'."""
    if tok.endswith(","):
        return False
    return not _YEAR.fullmatch(tok)


def _numeric_spans(line: str, min_start: int = 0) -> list[tuple[int, int, str]]:
    spans = []
    for m in _NUM.finditer(line):
        tok = m.group(0).strip()
        if not tok:
            continue
        start = m.start() + (len(m.group(0)) - len(m.group(0).lstrip()))
        if start >= min_start and _is_value_token(tok):
            spans.append((start, m.end(), tok))
    return spans


def _is_data_row(line: str, min_cols: int) -> bool:
    spans = _numeric_spans(line, min_start=26)  # values sit right of the label column
    return len(spans) >= min_cols


def _column_edges(lines: list[str], min_cols: int) -> list[tuple[int, int]]:
    """Infer column [start, end) ranges from right edges of numbers in data rows."""
    rights: list[int] = []
    for ln in lines:
        if _is_data_row(ln, min_cols):
            rights += [e for _, e, _ in _numeric_spans(ln, min_start=26)]
    if not rights:
        return []
    rights.sort()
    clusters: list[list[int]] = [[rights[0]]]
    for r in rights[1:]:
        if r - clusters[-1][-1] <= 4:
            clusters[-1].append(r)
        else:
            clusters.append([r])
    # keep clusters that appear in enough rows (real columns, not stray numbers inside labels)
    n_rows = sum(1 for ln in lines if _is_data_row(ln, min_cols))
    centers = [max(c) for c in clusters if len(c) >= max(2, n_rows // 3)]
    edges = []
    prev_end = 0
    for i, right in enumerate(centers):
        left = (centers[i - 1] + 1) if i else max(0, right - 22)
        edges.append((max(prev_end, left), right + 1))
        prev_end = right + 1
    return edges


def _assign(spans: list[tuple[int, int, str]], edges: list[tuple[int, int]]) -> list[str]:
    """Full rows are assigned left-to-right (robust to pdftotext's per-line horizontal shifts);
    rows with blank cells fall back to nearest-column-by-position."""
    n = len(edges)
    if len(spans) == n:
        return [tok for _, _, tok in spans]
    cells = [""] * n
    for _s, e, tok in spans:
        # column whose right edge is closest to the token's right edge
        idx = min(range(len(edges)), key=lambda i: abs(edges[i][1] - e))
        cells[idx] = (cells[idx] + " " + tok).strip()
    return cells


def parse_layout_table(text: str, *, min_cols: int = 2) -> Table | None:
    lines = [ln.rstrip() for ln in text.splitlines()]
    edges = _column_edges(lines, min_cols)
    if not edges:
        return None
    first_data = next(i for i, ln in enumerate(lines) if _is_data_row(ln, min_cols))
    label_limit = edges[0][0] - 1

    # headers: words in the lines above the first data row, bucketed by column overlap
    headers = [""] * len(edges)
    unit_note = None
    # header block = lines just above the first data row, stopping at a double blank line
    start, blanks = first_data, 0
    while start > 0:
        prev = lines[start - 1]
        blanks = blanks + 1 if not prev.strip() else 0
        if blanks >= 2:
            break
        start -= 1
    for ln in lines[:first_data]:
        m_unit = re.search(r"\(((?:all amounts|amounts|in |₹|rs)[^)]*)\)", ln, re.I)
        if m_unit and not unit_note:
            unit_note = m_unit.group(1)
    for ln in lines[start:first_data]:
        if re.search(r"\((?:all amounts|amounts|in |₹|rs)[^)]*\)", ln, re.I):
            continue
        chunks = [m for m in re.finditer(r"\S+(?:\s\S+)*", ln) if m.end() > label_limit]
        col_w = max(1, min(e - s for s, e in edges))
        if any(m.end() - m.start() > 1.3 * col_w for m in chunks) and not (
            len(chunks) == 1 and len(chunks[0].group(0)) > 45 and chunks[0].start() < label_limit
        ):
            # a chunk wider than a column spans two headers joined by a single space: place word by word
            for w in re.finditer(r"\S+", ln):
                if w.end() <= label_limit:
                    continue
                mid = (w.start() + w.end()) / 2
                idx = min(range(len(edges)), key=lambda i: abs((edges[i][0] + edges[i][1]) / 2 - mid))
                headers[idx] = (headers[idx] + " " + w.group(0)).strip()
            continue
        if len(chunks) == 1 and len(chunks[0].group(0)) > 45:
            continue  # a title line (company name, statement name), not a column header
        if len(chunks) == len(edges):  # one fragment per column: assign in order
            idxs = list(range(len(edges)))
        else:

            def nearest(m):
                mid = (m.start() + m.end()) / 2
                return min(range(len(edges)), key=lambda j: abs((edges[j][0] + edges[j][1]) / 2 - mid))

            idxs = [nearest(m) for m in chunks]
            if len(set(idxs)) < len(idxs):  # collision => line is shifted; place sequentially
                first = min(idxs[0], len(edges) - len(chunks))
                idxs = list(range(first, first + len(chunks)))
        for i, m in zip(idxs, chunks, strict=True):
            headers[i] = (headers[i] + " " + m.group(0)).strip()

    table = Table(
        headers=[re.sub(r"\s+", " ", h) for h in headers],
        unit_note=unit_note,
        periods=_periods(lines[start:first_data], edges, headers),
    )
    pending_label = ""
    for ln in lines[first_data:]:
        if not ln.strip():
            continue
        spans = _numeric_spans(ln, min_start=26)
        if len(spans) > len(edges):  # leading extras are note references printed in a Notes column
            spans = spans[-len(edges) :]
        if spans:
            label = (pending_label + " " + ln[: spans[0][0]].strip()).strip()
            note = re.search(r"\s+(\d{1,3}(?:\([a-z]\))?)$", label)
            if note and len(label) > 8:
                label = f"{label[: note.start()].strip()} [note {note.group(1)}]"
            label = re.sub(r"\s{2,}", " ", label)
            table.rows.append((label, _assign(spans, edges)))
            pending_label = ""
        else:
            stripped = ln.strip()
            if re.fullmatch(r"\d{1,3}", stripped):  # page number
                continue
            # label-only line: either a section heading or the first half of a wrapped label
            if table.rows and not pending_label and stripped[:1].islower():
                last_label, vals = table.rows[-1]
                table.rows[-1] = (f"{last_label} {stripped}", vals)
            else:
                pending_label = (pending_label + " " + stripped).strip() if pending_label else stripped
                if pending_label.isupper() or pending_label.endswith(":"):
                    table.rows.append((pending_label, [""] * len(edges)))
                    pending_label = ""
    return table


_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_DATE = re.compile(rf"\b({_MONTHS})\s+(\d{{1,2}}),?\s*(\d{{4}})?", re.I)
_MNUM = {m.lower(): i for i, m in enumerate(_MONTHS.split("|"), 1)}


def _periods(header_lines: list[str], edges: list[tuple[int, int]], headers: list[str]) -> list[str | None]:
    """Locate period-end dates in the header block by x-position.

    Handles dates printed whole ("March 31, 2026") and split over lines ("ended March" / "31," / "2026"),
    taking the day/year tokens that sit under the same column.
    """
    n = len(edges)

    def col_of(x: float) -> int:
        return min(range(n), key=lambda i: abs((edges[i][0] + edges[i][1]) / 2 - x))

    def tokens_below(li: int, col: int, pattern: str) -> str | None:
        for ln in header_lines[li + 1 : li + 4]:
            for t in re.finditer(pattern, ln):
                if col_of((t.start() + t.end()) / 2) == col:
                    return t.group(1) if t.groups() else t.group(0)
        return None

    found: list[str | None] = [None] * n
    for li, ln in enumerate(header_lines):
        for m in re.finditer(rf"\b({_MONTHS})\b(?:\s+(\d{{1,2}}),?)?(?:\s*((?:19|20)\d{{2}}))?", ln, re.I):
            col = col_of((m.start() + m.end()) / 2)
            if found[col]:
                continue
            day = m.group(2) or tokens_below(li, col, r"\b(\d{1,2}),")
            year = m.group(3) or tokens_below(li, col, r"\b((?:19|20)\d{2})\b")
            if day and year:
                found[col] = f"{year}-{_MNUM[m.group(1).lower()]:02d}-{int(day):02d}"
    out: list[str | None] = []
    for i, date in enumerate(found):
        if not date:
            out.append(None)
            continue
        h = headers[i].lower() if i < len(headers) else ""
        if re.search(r"three month|3 month|quarter", h):
            kind = "3M ended"
        elif re.search(r"nine month|9 month", h):
            kind = "9M ended"
        elif re.search(r"six month|half year|6 month", h):
            kind = "6M ended"
        elif re.search(r"\bas at\b|\bas on\b", h):
            kind = "as at"
        elif "year" in h or date[5:] == "03-31":
            kind = "FY ended"
        else:
            kind = "period ended"
        out.append(f"{kind} {date}")
    return out
