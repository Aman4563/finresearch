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


def _calc(fn, args, inputs, **extra):
    return {"fincalc": {"function": fn, "args": args}, "inputs": inputs, **extra}


def test_correct_fincalc_on_inputs_that_are_not_the_cited_claims_is_not_grade_d(env, tmp_path):
    # #265: (115 - 100) / 100 = 15% reproduces the stated 15%, but the cited claims are 200 and 230
    run_id = _run(tmp_path)
    fy25 = _save(run_id, [{"url": URL, "quote": "x"}], statement="FY25 revenue", value="200", period="FY2025")
    fy26 = _save(run_id, [{"url": URL, "quote": "y"}], statement="FY26 revenue", value="230")
    ids = [fy25["claim_id"], fy26["claim_id"]]
    growth = {"statement": "Revenue grew 15%", "metric": "revenue_growth", "value": "15", "unit": "%"}
    wrong = _save(run_id, [_calc("growth.pct_change", {"old": "100", "new": "115"}, ids)], **growth)
    chk = wrong["citation_checks"][0]
    assert chk["quote_found"] is False and "old, new match no cited input" in chk["match"]
    assert _grade(wrong["claim_id"]) == ("U", "unsupported")

    # the same claim twice: old=new=200 gives 0 %, stated as 0 %, but only one cited claim is 200
    same = _save(run_id, [_calc("growth.pct_change", {"old": "200", "new": "200"}, ids)],
                 **{**growth, "value": "0", "statement": "Revenue flat"})  # fmt: skip
    assert _grade(same["claim_id"]) == ("U", "unsupported")

    # the right inputs, given in the opposite order of the ids, bind
    good = _save(run_id, [_calc("growth.pct_change", {"new": 230, "old": 200}, ids[::-1])], **growth)
    assert _grade(good["claim_id"]) == ("D", "unverified")
    from finresearch.db import session_scope
    from finresearch.db.models import Claim

    with session_scope() as s:
        comp = s.get(Claim, good["claim_id"]).citations[0].computation
    assert comp["bindings"] == {"new": ids[1], "old": ids[0]} and comp["args_bound"] is True


def test_fincalc_arguments_bind_across_rupee_scales_and_percent_fractions(env, tmp_path):
    run_id = _run(tmp_path)
    # ₹1,234.5 crore = 1,234.5 x 10^7 = ₹12,345,000,000; ₹98.76 crore = ₹987,600,000
    rev = _save(run_id, [{"url": URL, "quote": "r"}])
    pat = _save(
        run_id, [{"url": URL, "quote": "p"}], statement="PAT ₹98.76 crore", metric="pat", value="98.76"
    )
    # 987,600,000 / 12,345,000,000 = 0.08 = 8 %
    margin = _save(run_id, [_calc("ratios.pat_margin", {"pat": "987600000", "revenue": "12345000000"},
                                  [rev["claim_id"], pat["claim_id"]])],
                   statement="PAT margin 8%", metric="pat_margin", value="8", unit="%")  # fmt: skip
    assert _grade(margin["claim_id"]) == ("D", "unverified")
    # a growth claim in % passed as a fraction: justified P/B (0.20 - 0.05) / (0.125 - 0.05) = 2
    roe = _save(run_id, [{"url": URL, "quote": "a"}], statement="ROE 20%", metric="roe", value="20", unit="%")
    coe = _save(
        run_id, [{"url": URL, "quote": "b"}], statement="CoE 12.5%", metric="coe", value="12.5", unit="%"
    )
    g = _save(run_id, [{"url": URL, "quote": "c"}], statement="g 5%", metric="g", value="5", unit="%")
    pb = _save(run_id, [_calc("valuation.justified_pb", {"roe": "0.20", "cost_of_equity": "0.125", "growth": "0.05"},
                              [roe["claim_id"], coe["claim_id"], g["claim_id"]])],
               statement="Justified P/B 2x", metric="justified_pb", value="2", unit="x")  # fmt: skip
    assert _grade(pb["claim_id"]) == ("D", "unverified")


def test_fincalc_argument_tolerance_edges(env, tmp_path):
    # REL_TOL 0.05 % of the larger value: |10005 - 10000| / 10005 = 0.04998 % binds; 6 / 10006 = 0.05996 % does not
    run_id = _run(tmp_path)
    a = _save(run_id, [{"url": URL, "quote": "a"}], statement="debt", metric="debt", value="10000")
    b = _save(run_id, [{"url": URL, "quote": "b"}], statement="cash", metric="cash", value="4000")
    ids = [a["claim_id"], b["claim_id"]]
    kw = {"statement": "net debt", "metric": "net_debt"}
    edge = _save(
        run_id, [_calc("ratios.net_debt", {"debt": "10005", "cash": "4000"}, ids)], value="6005", **kw
    )
    assert _grade(edge["claim_id"]) == ("D", "unverified")
    over = _save(
        run_id, [_calc("ratios.net_debt", {"debt": "10006", "cash": "4000"}, ids)], value="6006", **kw
    )
    assert _grade(over["claim_id"]) == ("U", "unsupported")
    # a sign is kept: cash -4000 is not the cited 4000
    neg = _save(
        run_id, [_calc("ratios.net_debt", {"debt": "10000", "cash": "-4000"}, ids)], value="14000", **kw
    )
    assert _grade(neg["claim_id"]) == ("U", "unsupported")
    # ...except for the arguments fincalc documents as positive magnitudes: capex printed (512.40) in the cash flow
    cfo = _save(run_id, [{"url": URL, "quote": "c"}], statement="CFO", metric="cfo", value="2000")
    capex = _save(run_id, [{"url": URL, "quote": "p"}], statement="Capex", metric="capex", value="-512.40")
    fcf = _save(run_id, [_calc("ratios.fcf", {"cfo": "2000", "capex": "512.40"}, [cfo["claim_id"], capex["claim_id"]])],
                statement="FCF", metric="fcf", value="1487.60")  # fmt: skip
    assert _grade(fcf["claim_id"]) == ("D", "unverified")


def test_fincalc_constants_must_be_declared_and_cannot_stand_alone(env, tmp_path):
    run_id = _run(tmp_path)
    fy23 = _save(
        run_id, [{"url": URL, "quote": "x"}], statement="FY23 revenue", value="1000", period="FY2023"
    )
    fy26 = _save(run_id, [{"url": URL, "quote": "y"}], statement="FY26 revenue", value="1331")
    ids = [fy23["claim_id"], fy26["claim_id"]]
    args = {"start": "1000", "end": "1331", "years": 3}  # (1331 / 1000) ** (1/3) - 1 = 0.10
    kw = {"statement": "3-year CAGR 10%", "metric": "revenue_cagr", "value": "10", "unit": "%"}
    undeclared = _save(run_id, [_calc("growth.cagr", args, ids)], **kw)
    assert "years match no cited input" in undeclared["citation_checks"][0]["match"]
    assert _grade(undeclared["claim_id"]) == ("U", "unsupported")
    declared = _save(run_id, [_calc("growth.cagr", args, ids, constants={"years": "FY2023 to FY2026: 3 years"})],
                     **kw)  # fmt: skip
    assert _grade(declared["claim_id"]) == ("D", "unverified")
    # every figure declared a constant (500 -> 665.5 is also 10 % a year) leans on no cited claim at all
    other = {"start": "500", "end": "665.5", "years": 3}
    alone = _save(run_id, [_calc("growth.cagr", other, ids, constants={k: "assumed" for k in other})], **kw)
    assert "constants only" in alone["citation_checks"][0]["match"]
    assert _grade(alone["claim_id"]) == ("U", "unsupported")
    with pytest.raises(ValueError, match="constants"):
        _save(run_id, [_calc("growth.cagr", args, ids, constants={"years": ""})], **kw)


def test_fincalc_list_arguments_need_one_cited_claim_per_element(env, tmp_path):
    run_id = _run(tmp_path)
    qs = [_save(run_id, [{"url": URL, "quote": str(v)}], statement=f"Q revenue {v}", value=str(v))["claim_id"]
          for v in (100, 110, 120, 130)]  # fmt: skip
    kw = {"statement": "TTM revenue 460", "metric": "revenue_ttm", "value": "460", "period": "TTM"}
    ok = _save(run_id, [_calc("growth.ttm", {"quarters": ["100", "110", "120", "130"]}, qs)], **kw)
    assert _grade(ok["claim_id"]) == ("D", "unverified")
    # 100 + 110 + 120 + 130 = 460 also as 100 + 100 + 130 + 130: two elements lean on claims already used
    dup = _save(run_id, [_calc("growth.ttm", {"quarters": ["100", "100", "130", "130"]}, qs)], **kw)
    chk = dup["citation_checks"][0]
    assert "quarters[" in chk["match"] and _grade(dup["claim_id"]) == ("U", "unsupported")


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
