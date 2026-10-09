"""Evidence grades (#242): web quotes checked against a stored page, fincalc citations re-run, the grade gate and the
ledger API. Recorded pages only: the fetcher is replaced, nothing touches the network."""

from __future__ import annotations

import hashlib
import json

import pytest

PAGE = """<html><head><title>t</title><script>var x = "Revenue grew 99%";</script></head><body>
<h1>Acme Cables results</h1><p>Revenue from operations was ₹ 1,234.5 crore in FY2026.</p>
<table><tr><td>EBITDA margin</td><td>12.4%</td></tr></table></body></html>"""
URL = "https://news.example.com/acme-results"


def _run(tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        co = get_or_create_company(s, "ev-" + tmp_path.name[-8:], "Acme Cables Ltd")
        r = ResearchRun(company_id=co.id, kind="ipo_report", manifest={})
        s.add(r)
        s.flush()
        return r.id


@pytest.fixture
def pages(monkeypatch):
    from finresearch.verify import web

    served: dict[str, str] = {URL: PAGE}

    async def fetcher(url):
        return served[url], "text/html; charset=utf-8"

    monkeypatch.setattr(web, "FETCHER", fetcher)
    return served


def _save(run_id, citations, **kw):
    from finresearch.db import session_scope
    from finresearch.mcp_server.claims import save_claim
    from finresearch.mcp_server.server import run_fincalc

    args = {"stream": "financials", "statement": kw.pop("statement", "Revenue was ₹1,234.5 crore"),
            "claim_type": "numeric", "metric": "revenue", "value": "1234.5", "unit": "INR crore", "period": "FY2026"}  # fmt: skip
    args.update(kw)
    with session_scope() as s:
        return save_claim(s, run_id=run_id, citations=citations, fincalc=run_fincalc, **args)


def _grade(claim_id):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.verify.evidence import claim_grade

    with session_scope() as s:
        c = s.get(Claim, claim_id)
        return claim_grade(c)["grade"], c.status


def test_html_to_text_drops_scripts_and_keeps_visible_text():
    from finresearch.verify.web import html_to_text

    text = html_to_text(PAGE)
    assert "Revenue from operations was ₹ 1,234.5 crore in FY2026." in text
    assert "99%" not in text and "var x" not in text
    assert "EBITDA margin" in text and "12.4%" in text


def test_web_quote_without_a_snapshot_is_unchecked_grade_c(env, tmp_path):
    run_id = _run(tmp_path)
    res = _save(run_id, [{"url": URL, "quote": "Revenue from operations was ₹ 1,234.5 crore"}])
    assert res["citation_checks"][0]["quote_found"] is None
    assert _grade(res["claim_id"]) == ("C", "unverified")


async def test_fetch_page_stores_the_text_and_web_quotes_are_checked_against_it(env, tmp_path, pages):
    from finresearch.mcp_server.server import fetch_page
    from finresearch.verify.web import html_to_text

    run_id = _run(tmp_path)
    out = json.loads(await fetch_page(URL, run_id))
    expected_sha = hashlib.sha256(html_to_text(PAGE).encode()).hexdigest()
    assert out["snapshot_sha256"] == expected_sha and out["next_offset"] is None
    assert "1,234.5 crore" in out["text"]

    ok = _save(run_id, [{"url": URL, "quote": "Revenue from operations was ₹ 1,234.5 crore in FY2026."}])
    assert ok["citation_checks"][0] == {"url": URL, "quote_found": True, "match": "exact (stored page)",
                                        "snapshot_sha256": expected_sha}  # fmt: skip
    assert _grade(ok["claim_id"]) == ("B", "unverified")

    # a quote that is not on the page (here: the text of a script the reader never sees) is caught
    bad = _save(run_id, [{"url": URL, "quote": "Revenue grew 99%"}])
    assert bad["citation_checks"][0]["quote_found"] is False
    assert _grade(bad["claim_id"]) == ("U", "unsupported")


async def test_fetch_page_refuses_private_addresses_and_pages_long_text(env, tmp_path, pages):
    from finresearch.mcp_server.server import fetch_page
    from finresearch.verify import web

    run_id = _run(tmp_path)
    assert "error" in json.loads(await fetch_page("http://127.0.0.1:8710/api/portfolio", run_id))
    long_url = "https://news.example.com/long"
    pages[long_url] = "x" * (web.PAGE_CHARS + 5)
    first = json.loads(await fetch_page(long_url, run_id))
    assert len(first["text"]) == web.PAGE_CHARS and first["next_offset"] == web.PAGE_CHARS
    rest = json.loads(await fetch_page(long_url, run_id, offset=first["next_offset"]))
    assert rest["text"] == "xxxxx" and rest["next_offset"] is None  # nothing is silently cut


def test_fincalc_citation_is_rerun_and_graded_d_only_when_it_reproduces_the_value(env, tmp_path):
    run_id = _run(tmp_path)
    fy25 = _save(run_id, [{"url": URL, "quote": "x"}], statement="FY25 revenue", value="200", period="FY2025")
    fy26 = _save(run_id, [{"url": URL, "quote": "y"}], statement="FY26 revenue", value="230")
    calc = {"fincalc": {"function": "growth.pct_change", "args": {"old": "200", "new": "230"}},
            "inputs": [fy25["claim_id"], fy26["claim_id"]]}  # fmt: skip
    # (230 - 200) / 200 = 0.15, stated as 15 %
    good = _save(run_id, [calc], statement="Revenue grew 15%", metric="revenue_growth", value="15", unit="%")
    assert good["citation_checks"][0]["quote_found"] is True
    assert _grade(good["claim_id"]) == ("D", "unverified")

    wrong = _save(run_id, [calc], statement="Revenue grew 18%", metric="revenue_growth", value="18", unit="%")
    assert wrong["citation_checks"][0]["quote_found"] is False
    assert _grade(wrong["claim_id"]) == ("U", "unsupported")

    no_inputs = _save(run_id, [{"fincalc": calc["fincalc"]}], statement="Revenue grew 15%", metric="revenue_growth",
                      value="15", unit="%")  # fmt: skip
    assert _grade(no_inputs["claim_id"]) == ("U", "unsupported")


def test_free_text_calculation_urls_are_rejected_and_legacy_ones_graded_u(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Citation, Claim
    from finresearch.verify.evidence import citation_grade

    run_id = _run(tmp_path)
    with pytest.raises(ValueError, match="http"):
        _save(run_id, [{"url": "fincalc: growth.cagr(6577, 11716, 2)", "quote": "33.5%"}])
    with session_scope() as s:  # a claim saved before #242 with a typed "fincalc:" note
        c = Claim(run_id=run_id, stream="financials", statement="CAGR 33.5%", claim_type="numeric")
        c.citations = [Citation(url="fincalc: growth.cagr(6577, 11716, 2)", quote="33.5%")]
        s.add(c)
        s.flush()
        assert citation_grade(c.citations[0]) == ("U", "calculation note, not re-checked")


def test_exchange_tool_responses_are_snapshots_too(env, tmp_path):
    from finresearch.mcp_server.server import _snapshot_response

    run_id = _run(tmp_path)
    src = "https://www.nseindia.com/api/ipo-detail?symbol=ACME"
    _snapshot_response(src, json.dumps({"qib": "12.53", "total": "8.10"}, indent=1))
    res = _save(run_id, [{"url": src, "quote": '"qib": "12.53"'}], statement="QIB 12.53x", metric="qib_times",
                value="12.53", unit="x", period="2026-09-28 17:00 IST")  # fmt: skip
    assert res["citation_checks"][0]["quote_found"] is True
    assert _grade(res["claim_id"])[0] == "B"


def test_publish_gate_needs_a_strong_grade_for_high_importance_claims(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.verify.gate import check_report

    run_id = _run(tmp_path)
    weak = _save(run_id, [{"url": "https://blog.example.com/acme", "quote": "Revenue was ₹1,234.5 crore"}],
                 importance="high")  # fmt: skip
    with session_scope() as s:
        s.get(Claim, weak["claim_id"]).status = "verified"  # a model said so; the evidence is still unchecked
    with session_scope() as s:
        g = check_report(s, run_id, f"Revenue ₹1,234.5 crore [C{weak['claim_id']}].")
    assert not g.ok and "grade C" in g.blocking[0]


async def test_publish_gate_passes_a_web_claim_checked_on_its_stored_page(env, tmp_path, pages):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.mcp_server.server import fetch_page
    from finresearch.verify.gate import check_report

    run_id = _run(tmp_path)
    await fetch_page(URL, run_id)
    strong = _save(
        run_id, [{"url": URL, "quote": "Revenue from operations was ₹ 1,234.5 crore"}], importance="high"
    )
    with session_scope() as s:
        s.get(Claim, strong["claim_id"]).status = "verified"
    with session_scope() as s:
        g = check_report(s, run_id, f"Revenue ₹1,234.5 crore [C{strong['claim_id']}].")
    assert g.ok, g.blocking


def test_ledger_api_returns_the_grade(env, tmp_path):
    from finresearch.api.app import claim_json
    from finresearch.db import session_scope
    from finresearch.db.models import Claim

    run_id = _run(tmp_path)
    res = _save(run_id, [{"url": URL, "quote": "q"}])
    with session_scope() as s:
        out = claim_json(s.get(Claim, res["claim_id"]), {})
    assert out["evidence_grade"] == "C" and out["evidence_label"] == "web source, quote not verified"
    assert out["citations"][0]["grade"] == "C" and out["citations"][0]["snapshot_sha256"] is None
