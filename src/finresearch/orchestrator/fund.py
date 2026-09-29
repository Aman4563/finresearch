"""The mutual-fund report: the research pipeline with fund streams and AMFI NAV facts.

A scheme is stored as a Company whose meta holds its AMFI scheme code (`amfi_code`); its NSE symbol is empty.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from finresearch.agents.roles import FUND_STREAMS
from finresearch.db import session_scope
from finresearch.db.models import Citation, Claim, Company, ResearchRun
from finresearch.orchestrator.base import ResearchPipeline

FUND_PIPELINE_VERSION = "fund-pipeline-1"
NAV_ALL = "https://www.amfiindia.com/spages/NAVAll.txt"
NAV_HISTORY = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"


class FundPipeline(ResearchPipeline):
    kind = "fund_report"
    version = FUND_PIPELINE_VERSION
    default_streams = FUND_STREAMS
    roles = {"planner": "fund_planner", "verifier": "verifier", "bull": "fund_bull",  # noqa: RUF012
             "bear": "fund_bear", "synthesizer": "fund_synthesizer", "critic": "fund_critic"}  # fmt: skip
    required_doc_kinds = ()  # AMFI data is enough to start; streams fetch factsheets and portfolios themselves
    subject = "an Indian mutual-fund scheme"
    primary_source = (
        "AMFI NAV data and the AMC's own scheme documents (SID/KIM, factsheet, portfolio disclosure)"
    )
    decision_deadline = "the investor's next review"

    def _load_context(self, require_offer_doc: bool = False):
        ctx = super()._load_context(require_offer_doc)
        with session_scope() as s:
            co = s.get(Company, s.get(ResearchRun, self.run_id).company_id)
            ctx.nse_symbol = (co.meta or {}).get("amfi_code")
        return ctx

    async def _facts(self) -> None:
        import json

        from finresearch.fincalc.dates import today_ist
        from finresearch.mcp_server.server import amfi_nav_history

        facts = dict(self.ctx.facts)
        facts["today_ist"] = today_ist().isoformat()
        code = self.ctx.nse_symbol
        if code and "fund" not in facts:
            try:
                data = json.loads(await amfi_nav_history(code, 5))
                if "error" in data:
                    raise ValueError(data["error"])
                facts["fund"] = {"scheme": data["scheme"], "stats": data["stats"], "first": data["first"],
                                 "last": data["last"]}  # fmt: skip
                self._once("fund_baseline", lambda: self._record(data, facts))
            except Exception as e:
                facts["fund_error"] = f"{type(e).__name__}: {e}"
        self.ctx.facts = facts
        self._update_manifest(facts=facts)

    def _record(self, data: dict[str, Any], facts: dict[str, Any]) -> None:
        with session_scope() as s:
            facts["baseline_claim_ids"] = record_fund_facts(s, self.run_id, data, datetime.now(UTC))


def _pct(fraction: str) -> str:
    from decimal import Decimal

    return str((Decimal(fraction) * 100).quantize(Decimal("0.0001")))


def fund_facts(data: dict[str, Any]) -> list[tuple[str, str, str, str, str, str, str]]:
    """(metric, value, unit, period, statement, url, quote) for the deterministic fund facts. Returns, volatility
    and drawdown are stored in % (the ledger keeps 6 decimals)."""
    sc, st, last = data["scheme"], data["stats"], data.get("last") or {}
    label = f"{sc['name']} ({sc['plan']}, {sc['option']})"
    out = [("nav", sc["nav"], "INR per unit", sc["nav_date"], f"{label} NAV ₹{sc['nav']} on {sc['nav_date']}",
            NAV_ALL, f"{sc['scheme_code']};{sc['name']};{sc['nav']};{sc['nav_date']}")]  # fmt: skip
    for y in (1, 3, 5):
        v = st.get(f"trailing_{y}y")
        if v is not None:
            out.append((f"return_{y}y_annualised", _pct(v), "%", f"{y}y to {last.get('date')}",
                        f"{label} {y}-year annualised return {_pct(v)}% (fincalc from AMFI NAVs)", NAV_HISTORY,
                        f"NAV history {sc['scheme_code']} to {last.get('date')} ({last.get('nav')})"))  # fmt: skip
    roll = st.get("rolling_3y") or {}
    for key in ("minimum", "median", "maximum"):  # live run 10 never recorded the tool's rolling statistics
        if roll.get(key) is not None:
            out.append((f"rolling_3y_return_{key}", _pct(roll[key]), "%", f"3y rolling windows within 5y to {last.get('date')}",
                        f"{label} {key} 3-year rolling return {_pct(roll[key])}% across {roll.get('count')} windows in "
                        "the last 5 years (fincalc from AMFI NAVs)", NAV_HISTORY, f"NAV history {sc['scheme_code']}"))  # fmt: skip
    for key, metric in (("annualised_volatility", "volatility_annualised"), ("max_drawdown", "max_drawdown")):
        if st.get(key) is not None:
            out.append((metric, _pct(st[key]), "%", f"5y to {last.get('date')}",
                        f"{label} {metric.replace('_', ' ')} {_pct(st[key])}% over 5 years (fincalc from AMFI NAVs)",
                        NAV_HISTORY,
                        f"NAV history {sc['scheme_code']}"))  # fmt: skip
    return out


def record_fund_facts(session, run_id: int, data: dict[str, Any], accessed_at: datetime) -> list[int]:
    from decimal import Decimal

    ids = []
    for metric, value, unit, period, statement, url, quote in fund_facts(data):
        c = Claim(run_id=run_id, stream="facts", statement=f"{statement} (AMFI)", claim_type="numeric",
                  metric=metric, value=Decimal(value), unit=unit, period=period,
                  importance="high" if metric.startswith(("nav", "return_3y", "return_5y")) else "normal",
                  status="verified", verifier_note="deterministic: AMFI NAV data (primary source) and fincalc",
                  checks={"source": "amfi"})  # fmt: skip
        session.add(c)
        session.flush()
        session.add(Citation(claim_id=c.id, url=url, accessed_at=accessed_at, quote=quote[:1000]))
        ids.append(c.id)
    session.flush()
    return ids


async def ensure_scheme_company(scheme_code: str, *, nav_all=None) -> dict[str, Any]:
    """Create (or refresh) the Company that stands for an AMFI scheme; returns slug, name and scheme details."""
    from finresearch.adapters.amfi import AmfiClient
    from finresearch.ingest.documents import get_or_create_company

    if nav_all is None:
        async with AmfiClient() as amfi:
            rows = await amfi.nav_all()
    else:
        rows = await nav_all()
    scheme = next((x for x in rows if x.code == scheme_code), None)
    if scheme is None:
        raise LookupError(f"scheme {scheme_code} is not in AMFI's NAV file")
    slug = f"mf-{scheme_code}"
    with session_scope() as s:
        co = get_or_create_company(s, slug, f"{scheme.name} ({scheme.plan}, {scheme.option})")
        co.meta = {
            **(co.meta or {}),
            "amfi_code": scheme_code,
            "category": scheme.category,
            "amc": scheme.amc,
        }
        return {"slug": slug, "name": co.name, "scheme_code": scheme_code, "category": scheme.category,
                "amc": scheme.amc}  # fmt: skip
