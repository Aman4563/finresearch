"""The AIS check against the database: the app's side from portfolio transactions, the stored AIS per financial year,
and the import entry point shared by the API and the statement inbox. Personal data: local only, never logged."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import PortfolioAis, PortfolioHolding, PortfolioTxn
from finresearch.fincalc.dates import fiscal_year
from finresearch.fincalc.tax import fy_label
from finresearch.portfolio.ais import AisItem, AisStatement, parse_ais_json
from finresearch.portfolio.ais_recon import AppEntry, reconcile
from finresearch.portfolio.importers import StatementError

_KIND = {"dividend": "dividend", "sell": "sale", "buy": "purchase"}


def app_entries(s: Session) -> list[AppEntry]:
    """Dividends, sells and buys at their gross value (units x price when the amount is missing); a sale's charges
    ride along so a charges-sized difference can be explained."""
    hs = {h.id: h for h in s.scalars(select(PortfolioHolding))}
    out = []
    for t in s.scalars(select(PortfolioTxn).where(PortfolioTxn.kind.in_(tuple(_KIND)))):
        h = hs.get(t.holding_id)
        if h is None:
            continue
        gross = abs(t.amount) if t.amount else (t.quantity * t.price if t.quantity and t.price else None)
        if not gross:
            continue
        out.append(AppEntry(h.id, h.name, h.isin, h.asset_type, _KIND[t.kind], t.day, gross, t.quantity,
                            t.charges or Decimal(0)))  # fmt: skip
    return out


def parse_file(content: bytes, password: str | None = None) -> AisStatement:
    """An AIS JSON or (password-protected) PDF. The password is used and forgotten: never stored or echoed."""
    if content.startswith(b"%PDF"):
        from finresearch.portfolio.ais_pdf import parse_ais_pdf

        if not password:
            raise StatementError("password: the AIS PDF password is your PAN in lower case followed by your date of "
                                 "birth as ddmmyyyy (e.g. abcde1234f01011990)")  # fmt: skip
        return parse_ais_pdf(content, password)
    return parse_ais_json(content)


def save(s: Session, st: AisStatement, fy: int, sha: str) -> PortfolioAis:
    row = s.get(PortfolioAis, fy) or PortfolioAis(fy=fy)
    row.sha256, row.format, row.ignored = sha, st.format, st.ignored
    row.items = [i.to_json() for i in st.items]
    row.warnings = list(st.warnings)
    row.imported_at = datetime.now(UTC)
    s.add(row)
    s.flush()
    return row


def check(s: Session, fy: int, items: list[AisItem] | None = None) -> dict[str, Any]:
    """The reconciliation for one year: the stored AIS (or `items`, for a dry run) against the app."""
    if items is None:
        row = s.get(PortfolioAis, fy)
        if row is None:
            raise LookupError(f"no AIS imported for {fy_label(fy)}")
        items = [AisItem.from_json(d) for d in row.items]
    return reconcile(items, app_entries(s), fy)


def statements(s: Session) -> dict[str, Any]:
    rows = list(s.scalars(select(PortfolioAis).order_by(PortfolioAis.fy.desc())))
    years = {
        fiscal_year(d) for d in s.scalars(select(PortfolioTxn.day).where(PortfolioTxn.kind.in_(tuple(_KIND))))
    }
    return {"statements": [{"fy": r.fy, "label": fy_label(r.fy), "format": r.format, "rows": len(r.items or []),
                            "ignored": r.ignored, "imported_at": r.imported_at.isoformat() if r.imported_at else None,
                            "warnings": r.warnings or []} for r in rows],
            "app_years": [{"fy": y, "label": fy_label(y)} for y in sorted(years, reverse=True)]}  # fmt: skip


def known_sha(s: Session, sha: str) -> int | None:
    return s.scalar(select(PortfolioAis.fy).where(PortfolioAis.sha256 == sha))
