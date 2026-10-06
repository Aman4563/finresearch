"""The AIS check against the database: the app's side from portfolio transactions, the stored AIS per financial year,
and the import entry point shared by the API and the statement inbox. Personal data: local only, never logged."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import PortfolioAis, PortfolioHolding, PortfolioSetting, PortfolioTxn
from finresearch.fincalc.dates import fiscal_year
from finresearch.fincalc.tax import fy_label
from finresearch.portfolio.ais import AisItem, AisStatement, parse_ais_json
from finresearch.portfolio.ais_recon import COMPARED, AppEntry, reconcile, within
from finresearch.portfolio.importers import StatementError

_KIND = {"dividend": "dividend", "sell": "sale", "buy": "purchase"}
# Set once per format (json | pdf) by the first saved import that validates (#216); never cleared. Until then the AIS
# card calls the check "Experimental": the parsers were built from field names and a summary-row pattern, not from a
# real downloaded AIS.
VALIDATED_KEY = "ais_format_validated"


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
    record_validation(s, st, fy)
    return row


def validation_gaps(st: AisStatement, result: dict[str, Any]) -> list[str]:
    """Why a saved import does not (yet) validate the format; empty = it does. Validated means: every row the parser
    met was understood (no unrecognised rows), at least one AIS row was compared, and for each compared category (dividend,
    sale, purchase) the AIS total equals the app's total within the check's own tolerance."""
    gaps = []
    if st.unrecognised:
        gaps.append(f"{st.unrecognised} row(s) could not be recognised")
    if not any(r.get("category") in COMPARED and r.get("ais_amount") for r in result.get("rows", [])):
        gaps.append("no dividend, sale or purchase row in the AIS to compare")
    for c, tot in (result.get("totals") or {}).items():
        ais, app = Decimal(str(tot.get("ais") or 0)), Decimal(str(tot.get("app") or 0))
        if not within(ais, app):
            gaps.append(f"{c} totals differ")
    return gaps


def record_validation(s: Session, st: AisStatement, fy: int) -> dict[str, Any] | None:
    """Mark the statement's format as validated the first time a saved import passes `validation_gaps`."""
    row = s.get(PortfolioSetting, VALIDATED_KEY)
    done = dict(row.value or {}) if row else {}
    if st.format in done or validation_gaps(st, check(s, fy, st.items)):
        return None
    done[st.format] = {"validated_at": datetime.now(UTC).isoformat(), "fy": fy}
    if row is None:
        s.add(PortfolioSetting(key=VALIDATED_KEY, value=done))
    else:
        row.value = done
    s.flush()
    return done[st.format]


def validated(s: Session) -> dict[str, Any]:
    """{format: {"validated_at", "fy"}} for the formats a real import has validated (#216)."""
    row = s.get(PortfolioSetting, VALIDATED_KEY)
    return dict(row.value or {}) if row else {}


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
            "app_years": [{"fy": y, "label": fy_label(y)} for y in sorted(years, reverse=True)],
            "validated": validated(s)}  # fmt: skip


def known_sha(s: Session, sha: str) -> int | None:
    return s.scalar(select(PortfolioAis.fy).where(PortfolioAis.sha256 == sha))
