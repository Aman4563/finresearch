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
    # table rows: every number in the quote present in the window?
    nums = re.findall(r"\(?\d[\d,]*\.?\d*\)?", quote)
    if nums and all(_loose(n) in _loose(window) for n in nums) and len(nums) >= 2:
        return True, "all quoted numbers present"
    return False, "quote not found in cited lines"


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
    session.add(claim)
    session.flush()
    checks = []
    for c in citations:
        if c.get("document_id"):
            doc = session.get(Document, int(c["document_id"]))
            if doc is None:
                raise ValueError(f"unknown document_id {c['document_id']}")
            ls, le = int(c["line_start"]), int(c.get("line_end") or c["line_start"])
            quote = (c.get("quote") or "").strip()
            found, how = quote_in_lines(doc, ls, le, quote) if quote else (False, "no quote given")
            page = _page_of(session, doc.id, ls)
            session.add(
                Citation(
                    claim_id=claim.id,
                    document_id=doc.id,
                    page_no=page,
                    line_start=ls,
                    line_end=le,
                    quote=quote,
                    quote_found=found,
                )
            )
            checks.append(
                {
                    "document_id": doc.id,
                    "lines": f"{ls}-{le}",
                    "page": page,
                    "quote_found": found,
                    "match": how,
                }
            )
        elif c.get("url"):
            accessed = c.get("accessed_at")
            session.add(
                Citation(
                    claim_id=claim.id,
                    url=c["url"],
                    quote=c.get("quote"),
                    accessed_at=datetime.fromisoformat(accessed) if accessed else datetime.now(UTC),
                )
            )
            checks.append({"url": c["url"], "quote_found": None, "match": "web source (verified later)"})
        else:
            raise ValueError("each citation needs document_id+line_start(+line_end)+quote, or url")
    doc_checks = [x for x in checks if "document_id" in x]
    if doc_checks and not any(x["quote_found"] for x in doc_checks):
        claim.status = "unsupported"
        claim.verifier_note = "no cited quote was found at the cited lines"
    session.flush()
    return {"claim_id": claim.id, "status": claim.status, "citation_checks": checks}


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
