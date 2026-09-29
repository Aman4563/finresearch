"""The listed-bond report: the research pipeline with bond streams and NSE listing facts.

A bond is stored as a Company with slug `bond-<isin>` whose meta holds the ISIN and NSE symbol/series.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from finresearch.agents.roles import BOND_STREAMS
from finresearch.db import session_scope
from finresearch.db.models import Citation, Claim, Company, ResearchRun
from finresearch.orchestrator.base import ResearchPipeline

BOND_PIPELINE_VERSION = "bond-pipeline-1"
BONDS_PAGE = "https://www.nseindia.com/market-data/bonds-traded-in-capital-market"


class BondPipeline(ResearchPipeline):
    kind = "bond_report"
    version = BOND_PIPELINE_VERSION
    default_streams = BOND_STREAMS
    roles = {"planner": "bond_planner", "verifier": "verifier", "bull": "bond_bull",  # noqa: RUF012
             "bear": "bond_bear", "synthesizer": "bond_synthesizer", "critic": "bond_critic"}  # fmt: skip
    required_doc_kinds = ()
    subject = "an Indian listed bond (NCD)"
    primary_source = "the bond's offer document or information memorandum and the rating agencies' releases"
    decision_deadline = "the investor's next review (before the next coupon record date)"

    def _load_context(self, require_offer_doc: bool = False):
        ctx = super()._load_context(require_offer_doc)
        with session_scope() as s:
            co = s.get(Company, s.get(ResearchRun, self.run_id).company_id)
            ctx.nse_symbol = (co.meta or {}).get("isin")
        return ctx

    async def _facts(self) -> None:
        from finresearch.adapters.nse_bonds import live_bonds
        from finresearch.fincalc.dates import today_ist

        facts = dict(self.ctx.facts)
        facts["today_ist"] = today_ist().isoformat()
        isin = self.ctx.nse_symbol
        if isin and "bond" not in facts:
            try:
                bond = next((b for b in await live_bonds() if b.isin.upper() == isin.upper()), None)
                if bond is None:
                    raise ValueError(f"{isin} is not in NSE's list of traded bonds")
                facts["bond"] = {**bond.model_dump(mode="json"), "warnings": bond.warnings}
                self._once("bond_baseline", lambda: self._record(bond, facts))
            except Exception as e:
                facts["bond_error"] = f"{type(e).__name__}: {e}"
        self.ctx.facts = facts
        self._update_manifest(facts=facts)

    def _record(self, bond, facts: dict[str, Any]) -> None:
        with session_scope() as s:
            facts["baseline_claim_ids"] = record_bond_facts(s, self.run_id, bond, datetime.now(UTC))


def bond_facts(bond) -> list[tuple[str, Any, str, str, str]]:
    """(metric, value, unit, period, statement) for the NSE listing facts of a bond."""
    asof = bond.as_of.strftime("%Y-%m-%d %H:%M IST") if bond.as_of else "latest"
    out = []
    if bond.coupon_pct is not None:
        out.append(
            (
                "coupon_rate",
                bond.coupon_pct,
                "%",
                "per annum",
                f"{bond.symbol} coupon {bond.coupon_pct}% a year",
            )
        )
    if bond.face_value is not None:
        out.append(
            ("face_value", bond.face_value, "INR", "per bond", f"{bond.symbol} face value ₹{bond.face_value}")
        )
    if bond.last_price is not None:
        out.append(
            (
                "last_price",
                bond.last_price,
                "INR",
                asof,
                f"{bond.symbol} last traded at ₹{bond.last_price} ({asof})",
            )
        )
    if bond.maturity is not None:
        out.append(("maturity_year", bond.maturity.year, "year", bond.maturity.isoformat(),
                    f"{bond.symbol} matures on {bond.maturity}"))  # fmt: skip
    return out


def record_bond_facts(session, run_id: int, bond, accessed_at: datetime) -> list[int]:
    from decimal import Decimal

    ids = []
    for metric, value, unit, period, statement in bond_facts(bond):
        c = Claim(run_id=run_id, stream="facts", statement=f"{statement} (NSE)", claim_type="numeric", metric=metric,
                  value=Decimal(str(value)), unit=unit, period=period, importance="high", status="verified",
                  verifier_note="deterministic: NSE list of bonds traded in the capital market", checks={"source": "nse_bonds"})  # fmt: skip
        session.add(c)
        session.flush()
        session.add(Citation(claim_id=c.id, url=BONDS_PAGE, accessed_at=accessed_at,
                             quote=f"{bond.symbol} {bond.series} {bond.isin} coupon {bond.coupon_pct} face {bond.face_value} "
                                   f"LTP {bond.last_price} maturity {bond.maturity}"))  # fmt: skip
        ids.append(c.id)
    session.flush()
    return ids


async def ensure_bond_company(isin: str, *, bonds=None) -> dict[str, Any]:
    from finresearch.adapters.nse_bonds import live_bonds
    from finresearch.ingest.documents import get_or_create_company

    rows = await bonds() if bonds else await live_bonds()
    bond = next((b for b in rows if b.isin.upper() == isin.upper()), None)
    if bond is None:
        raise LookupError(f"{isin} is not in NSE's list of traded bonds")
    slug = f"bond-{bond.isin.lower()}"
    with session_scope() as s:
        co = get_or_create_company(
            s, slug, f"{bond.symbol} {bond.series or ''} ({bond.coupon_pct}% maturing {bond.maturity})"
        )
        co.meta = {**(co.meta or {}), "isin": bond.isin, "symbol": bond.symbol, "series": bond.series}
        return {"slug": slug, "name": co.name, "isin": bond.isin}
