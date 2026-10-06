"""Claim ledger operations + deterministic citation checks (no LLM involved)."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from finresearch.db.models import Citation, Claim, Document, ResearchRun
from finresearch.ingest.text import read_lines

CLAIM_TYPES = {"numeric", "factual", "opinion"}
IMPORTANCE = {"high", "normal", "low"}
DEVANAGARI = re.compile(r"[\u0900-\u097F]")
TRANSLATION_MARK = re.compile(r"translat|अनुवाद", re.I)
LINE_TOLERANCE = 2  # allow the quote to sit up to 2 lines outside the cited span (wrapped lines)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).replace("\f", " ")
    s = (
        s.replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("–", "-")
        .replace("—", "-")
    )
    return re.sub(r"\s+", " ", s).strip().lower()


def _loose(s: str) -> str:
    """Whitespace-, comma- and currency-insensitive form (tables re-flow; '4,891.56' vs '4891.56')."""
    return re.sub(r"[\s,₹]", "", _norm(s))


def quote_in_lines(doc: Document, line_start: int, line_end: int, quote: str) -> tuple[bool, str]:
    """True if `quote` occurs within [line_start-2, line_end+2] of the document's canonical text."""
    lines = read_lines(doc.text_path)
    if not (1 <= line_start <= line_end <= len(lines)):
        return False, f"line range {line_start}-{line_end} outside document (1-{len(lines)})"
    a, b = max(1, line_start - LINE_TOLERANCE), min(len(lines), line_end + LINE_TOLERANCE)
    window = "\n".join(lines[a - 1 : b])
    if _norm(quote) in _norm(window):
        return True, "exact"
    if _loose(quote) and _loose(quote) in _loose(window):
        return True, "loose (whitespace/commas)"
    # table rows: every number in the quote present in the window as a whole number (not a substring: "12" must not
    # match inside "1,234", nor "2.5" inside "12.5")
    nums = _num_tokens(quote)
    # the window is also read with "1, 234" rejoined (pdftotext can split a grouped number after its comma)
    in_window = set(_num_tokens(window)) | set(_num_tokens(re.sub(r"(?<=\d),\s+(?=\d)", ",", window)))
    if len(nums) >= 2 and set(nums) <= in_window:
        # a figure quoted as negative ("-512.40", "(512.40)") must be printed negative there too (issue #217)
        if _negatives(quote) <= _negatives(window) | _negatives(re.sub(r"(?<=\d),\s+(?=\d)", ",", window)):
            return True, "all quoted numbers present"
        return False, "a number quoted as negative is not negative in the cited lines"
    return False, "quote not found in cited lines"


def _negatives(s: str) -> set[Decimal]:
    from finresearch.verify.values import tokens

    return {abs(t.value) for ln in s.split("\n") for t in tokens(ln) if t.neg}


_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _num_tokens(s: str) -> list[str]:
    """Whole numbers in `s` with their grouping commas dropped ("1,234.5" -> "1234.5", "12,34,567" -> "1234567")."""
    return [m.group(0).rstrip(",").replace(",", "") for m in _NUM_TOKEN.finditer(_norm(s))]


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(",", ""))
    except InvalidOperation as e:
        raise ValueError(f"value must be numeric, got {v!r}") from e


def save_claim(
    session: Session,
    *,
    run_id: int,
    stream: str,
    statement: str,
    claim_type: str,
    citations: list[dict[str, Any]],
    metric: str | None = None,
    value: Any = None,
    unit: str | None = None,
    period: str | None = None,
    importance: str = "normal",
) -> dict[str, Any]:
    if claim_type not in CLAIM_TYPES:
        raise ValueError(f"claim_type must be one of {sorted(CLAIM_TYPES)}")
    if importance not in IMPORTANCE:
        raise ValueError(f"importance must be one of {sorted(IMPORTANCE)}")
    if session.get(ResearchRun, run_id) is None:
        raise ValueError(f"unknown run_id {run_id}")
    if claim_type != "opinion" and not citations:
        raise ValueError("numeric/factual claims need at least one citation")
    if claim_type == "numeric":
        missing = [
            k
            for k, v in (("metric", metric), ("value", value), ("unit", unit), ("period", period))
            if v in (None, "")
        ]
        if missing:
            raise ValueError(
                f"numeric claims are atomic: one figure with metric, value, unit and period (missing {missing}). "
                "Save each figure of a table as its own claim."
            )

    # every citation is checked before anything is written: a bad one must not leave a half-saved claim behind
    # (the agent fixes the citation and saves again, which would duplicate it)
    parsed: list[tuple[Citation, dict[str, Any]]] = []
    for c in citations:
        if c.get("document_id"):
            try:
                doc_id = int(c["document_id"])
                ls = int(c["line_start"])
                le = int(c.get("line_end") or ls)
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(
                    f"a document citation needs integer document_id and line_start: {c!r}"
                ) from e
            doc = session.get(Document, doc_id)
            if doc is None:
                raise ValueError(f"unknown document_id {c['document_id']}")
            quote = (c.get("quote") or "").strip()
            found, how = quote_in_lines(doc, ls, le, quote) if quote else (False, "no quote given")
            page = _page_of(session, doc.id, ls)
            parsed.append((Citation(document_id=doc.id, page_no=page, line_start=ls, line_end=le, quote=quote,
                                    quote_found=found),
                           {"document_id": doc.id, "lines": f"{ls}-{le}", "page": page, "quote_found": found,
                            "match": how}))  # fmt: skip
        elif c.get("url"):
            accessed = c.get("accessed_at")
            try:
                at = datetime.fromisoformat(accessed) if accessed else datetime.now(UTC)
            except (TypeError, ValueError) as e:
                raise ValueError(f"accessed_at must be an ISO date-time, got {accessed!r}") from e
            parsed.append((Citation(url=c["url"], quote=c.get("quote"), accessed_at=at),
                           {"url": c["url"], "quote_found": None, "match": "web source (verified later)"}))  # fmt: skip
        else:
            raise ValueError("each citation needs document_id+line_start(+line_end)+quote, or url")

    claim = Claim(
        run_id=run_id,
        stream=stream,
        statement=statement,
        claim_type=claim_type,
        metric=metric,
        value=_dec(value),
        unit=unit,
        period=period,
        importance=importance,
    )
    claim.citations = [cit for cit, _ in parsed]
    session.add(claim)
    session.flush()
    checks = [chk for _, chk in parsed]
    doc_checks = [x for x in checks if "document_id" in x]
    if doc_checks and not any(x["quote_found"] for x in doc_checks):
        claim.status = "unsupported"
        claim.verifier_note = "no cited quote was found at the cited lines"
    out: dict[str, Any] = {"claim_id": claim.id, "status": claim.status, "citation_checks": checks}
    if any(DEVANAGARI.search(c.get("quote") or "") for c in citations):
        # live run 7: the agent kept the Hindi quote but did not mark its English statement as a translation, even
        # when told to, so the marker is added deterministically
        auto = not TRANSLATION_MARK.search(statement)
        if auto:
            claim.statement = f"{statement.rstrip()} (translated from Hindi)"
            out["note"] = (
                "Hindi source: the statement was marked '(translated from Hindi)'; keep the quote in Hindi."
            )
        claim.checks = {**(claim.checks or {}), "source_language": "hi", "translation_marked": True,
                        "translation_mark_added": auto}  # fmt: skip
    session.flush()
    return out


def _page_of(session: Session, document_id: int, line: int) -> int | None:
    from sqlalchemy import select

    from finresearch.db.models import DocumentPage

    return session.scalar(
        select(DocumentPage.page_no).where(
            DocumentPage.document_id == document_id,
            DocumentPage.line_start <= line,
            DocumentPage.line_end >= line,
        )
    )
