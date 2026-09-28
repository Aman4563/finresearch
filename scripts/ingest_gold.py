"""Ingest the two manually-researched IPO packs (Moneyview, Orient Cables) as the gold evaluation set."""

from __future__ import annotations

import re
import time
from pathlib import Path

from finresearch.bridge import Tier, build_router
from finresearch.config import get_settings
from finresearch.db import session_scope
from finresearch.ingest.documents import DocKind, get_or_create_company, ingest_pdf
from finresearch.ingest.index import index_document

ROOT = Path(__file__).resolve().parents[2]
PACKS = {
    "moneyview": ("Moneyview Limited", "MONEYVIEW", ROOT / "Moneyview_IPO_Research"),
    "orient-cables": ("Orient Cables (India) Limited", "ORIENTCABL", ROOT / "OrientCables_IPO_Research"),
}
RULES = [
    (r"_DRHP_", DocKind.DRHP),
    (r"_RHP_", DocKind.RHP),
    (r"Abridged", DocKind.ABRIDGED),
    (r"Corrigendum|Addendum", DocKind.ADDENDUM),
    (r"Price_Band", DocKind.PRICE_BAND_AD),
    (r"Anchor", DocKind.ANCHOR),
    (r"Annual_Report", DocKind.ANNUAL_REPORT),
    (r"Industry_Report", DocKind.INDUSTRY_REPORT),
    (r"Restated|Audited|Financials|Tax_Benefits|Networks|Bedrock", DocKind.FINANCIALS),
]


def kind_for(name: str) -> DocKind:
    return next((k for rx, k in RULES if re.search(rx, name)), DocKind.OTHER)


def main() -> None:
    s = get_settings()
    emb = build_router().engines[Tier.LOCAL]
    for slug, (name, sym, folder) in PACKS.items():
        pdfs = sorted(p for p in folder.rglob("*.pdf") if "06_Final_Report" not in p.parts)
        for pdf in pdfs:
            t = time.time()
            with session_scope() as db:
                co = get_or_create_company(db, slug, name, nse_symbol=sym)
                doc = ingest_pdf(
                    db, pdf, company=co, kind=kind_for(pdf.name), title=pdf.stem, docs_dir=s.docs_dir
                )
                n = index_document(db, doc, embedder=emb)
                print(
                    f"[{slug}] {pdf.name}: doc {doc.id} {doc.kind} {doc.pages}p ({doc.scanned_pages} OCR) "
                    f"{n} chunks in {time.time() - t:.0f}s",
                    flush=True,
                )


if __name__ == "__main__":
    main()
