"""Report insights: chart-ready figures derived from the claim ledger without a model (pure functions + the route)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from finresearch.api.insights import (
    build_insights,
    fmt_num,
    fmt_value,
    norm_cites,
    norm_unit,
    parse_period,
    ranked_risks,
)

_ids = iter(range(1, 10_000))


def cl(metric, value, unit, period, *, stream="financials", status="verified", statement=None, corrects=None,
       claim_type="numeric", importance="high", id=None):  # fmt: skip
    return {"id": id or next(_ids), "stream": stream, "metric": metric, "value": value, "unit": unit, "period": period,
            "status": status, "statement": statement or f"{metric} {value}", "claim_type": claim_type,
            "importance": importance, "corrects_claim_id": corrects, "citations": []}  # fmt: skip


def series(out, key, freq="annual"):
    return next(s for s in out["financials"]["series"] if s["key"] == key and s["freq"] == freq)


# --------------------------------------------------------------------------- parsing
@pytest.mark.parametrize(("raw", "label", "freq"), [
    ("FY2026", "FY26", "annual"), ("FY26", "FY26", "annual"), ("FY2026, consolidated", "FY26", "annual"),
    ("FY2026 (as at 2026-03-31)", "FY26", "annual"), ("Q1 FY2027", "Q1 FY27", "quarterly"),
    ("Q1 FY27 (quarter ended 2026-06-30), consolidated", "Q1 FY27", "quarterly"),
    ("Q4 FY26 (Jan-Mar 2026)", "Q4 FY26", "quarterly"),
])  # fmt: skip
def test_fiscal_periods_are_normalised(raw, label, freq):
    p = parse_period(raw)
    assert p is not None and (p.label, p.freq) == (label, freq)


@pytest.mark.parametrize("raw", ["FY2024-FY2026", "FY22–FY26", "TTM Q2FY26-Q1FY27", "2026-06-30", "FY2026 (standalone)",
                                 "FY2025 Q1 (30-Jun-2024)", "Q1 FY27 (LTM)", "post-repayment pro forma, FY2027",
                                 "FY2027E (1-year forward)", "FY2026 vs FY2025", None, ""])  # fmt: skip
def test_ranges_ttm_dates_and_estimates_are_not_series_points(raw):
    assert parse_period(raw) is None


def test_units_keep_rupee_scales_and_show_fractions_as_percent():
    assert norm_unit("INR million") == ("₹ Mn", 1) and norm_unit("INR crore") == ("₹ Cr", 1)
    assert norm_unit("fraction") == ("%", 100) and norm_unit("fraction (30% slab)") == ("%", 100)
    assert norm_unit("x (average industry P/E)") == ("x", 1)
    assert (
        norm_unit("USD million") == ("US$ Mn", 1) and norm_unit("widgets") is None and norm_unit(None) is None
    )


def test_indian_grouping_and_small_multiples():
    assert fmt_num(Decimal("178650")) == "1,78,650" and fmt_num(Decimal("-988.62")) == "-988.62"
    assert fmt_num(Decimal("4071263063049.6")) == "40,71,26,30,63,049.6"
    assert fmt_value(Decimal("0.0574"), "x") == "0.057x"  # a thin QIB book must not round up to 0.06x
    assert fmt_value(Decimal("3200"), "₹ Mn") == "₹3,200 Mn" and fmt_value(Decimal("12.5"), "%") == "12.5%"


def test_bare_and_parenthesised_claim_refs_become_chips():
    assert (
        norm_cites("inside ₹889–1,000 (C1626/C1627). Q2 (C1440)")
        == "inside ₹889–1,000 [C1626][C1627]. Q2 [C1440]"
    )
    assert norm_cites("already [C12] and C1328 here") == "already [C12] and [C1328] here"


# --------------------------------------------------------------------------- the ledger rules
def test_only_usable_uncorrected_claims_reach_a_series():
    wrong = cl("revenue_from_operations", "999", "INR million", "FY2025", id=5001)
    claims = [
        cl("revenue_from_operations", "6577.67", "INR million", "FY2024"),
        wrong,
        cl(
            "revenue_from_operations",
            "8249.58",
            "INR million",
            "FY2025",
            corrects=5001,
            status="needs_review",
        ),
        cl("revenue_from_operations", "11716.54", "INR million", "FY2026", status="contradicted"),
        cl("revenue_from_operations", "11000", "INR million", "FY2026", status="unsupported"),
        cl("revenue_from_operations", "10000", "INR million", "FY2026", status="unverified"),
    ]
    out = build_insights(run_id=1, kind="ipo_report", claims=claims, synthesis=None, report_markdown="")
    pts = series(out, "revenue")["points"]
    assert [(p["period"], p["raw"], p["status"]) for p in pts] == [("FY24", "6577.67", "verified"),
                                                                   ("FY25", "8249.58", "needs_review")]  # fmt: skip
    assert all(p["claim_id"] != 5001 for p in pts)


def test_families_prefer_aliases_and_split_currencies():
    claims = [
        cl("profit_attributable_to_owners", "26713", "INR crore", "FY2025", stream="stock_fundamentals"),
        cl("profit_attributable_to_owners", "29440", "INR crore", "FY2026", stream="stock_fundamentals"),
        cl("profit_for_the_year", "29474", "INR crore", "FY2026, consolidated", stream="stock_fundamentals"),
        cl("revenue_from_operations", "162990", "INR crore", "FY2025", stream="stock_fundamentals"),
        cl("revenue_from_operations", "178650", "INR crore", "FY2026", stream="stock_fundamentals"),
        cl("revenue_from_operations", "20158", "USD million", "FY2026", stream="stock_fundamentals"),
        cl("basic_eps", "64.5", "INR", "FY2025", stream="stock_fundamentals"),
        cl("basic_eps", "71.58", "INR per share", "FY2026", stream="stock_fundamentals"),
        cl(
            "revenue_from_operations", "1", "INR crore", "FY2020", stream="stock_news"
        ),  # not a financial stream
    ]
    out = build_insights(run_id=1, kind="stock_report", claims=claims, synthesis=None, report_markdown="")
    pat = series(out, "pat")
    assert [p["raw"] for p in pat["points"]] == ["26713", "29440"]  # owners' profit beats profit for the year
    rev_units = {
        s["unit"]: [p["period"] for p in s["points"]]
        for s in out["financials"]["series"]
        if s["key"] == "revenue"
    }
    assert rev_units == {"₹ Cr": ["FY25", "FY26"], "US$ Mn": ["FY26"]}  # never on one axis
    eps = series(out, "eps")
    assert eps["unit"] == "₹/share" and [p["raw"] for p in eps["points"]] == ["64.5", "71.58"]
    tiles = {t["label"]: t for t in out["key_numbers"]}
    assert tiles["Revenue"]["delta"] == {
        "pct": 9.61,
        "vs": "FY25",
        "claim_ids": [claims[3]["id"], claims[4]["id"]],
    }


def test_fraction_values_are_shown_as_percent_with_the_raw_value_kept():
    claims = [cl("return_5y_annualised", "12.7642", "%", "5y to 2026-09-28", stream="facts"),
              cl("benchmark_return_5y", "0.169", "fraction", "as of 2026-09-16", stream="fund_performance",
                 status="needs_review")]  # fmt: skip
    out = build_insights(run_id=1, kind="fund_report", claims=claims, synthesis=None, report_markdown="")
    row = out["fund"]["returns"][0]
    assert row["horizon"] == "5Y" and row["fund"]["display"] == "12.76%"
    assert row["benchmark"]["value"] == 16.9 and row["benchmark"]["raw"] == "0.169"
    assert row["benchmark"]["status"] == "needs_review"
    story = " ".join(out["plain_english"])
    assert "12.76%" in story and "16.9%" not in story  # a needs-review benchmark is charted, not a headline


def test_peer_names_come_from_statements_with_fallbacks():
    rr = cl(
        "peer_pe",
        "54.85",
        "x",
        "FY2026",
        stream="valuation",
        id=6001,
        statement="RR Kabel Limited (RHP peer set): FY2026 diluted EPS ₹43.52, P/E 54.85x",
    )
    claims = [
        rr,
        cl(
            "peer_pe",
            "46.69",
            "x",
            "FY2026",
            stream="valuation",
            statement="Polycab India Limited (RHP peer set): P/E",
        ),
        cl(
            "peer_pe_ttm",
            "15.29",
            "x",
            "2026-09-19",
            stream="stock_valuation",
            status="needs_review",
            statement="TCS trailing P/E (TTM) was 15.29x",
        ),
        cl(
            "peer_pe",
            "55",
            "x",
            "FY2026",
            stream="valuation",
            corrects=6001,
            id=6002,
            statement="[correction of C6001] 55.00x on the 7-Sep close",
        ),
        cl("pe_basic_fy26", "51.61", "x", "cap price ₹272", stream="valuation"),
        cl("pe_basic_fy26", "48.96", "x", "floor price ₹258", stream="valuation", id=6003),
    ]
    out = build_insights(run_id=1, kind="ipo_report", claims=claims, synthesis=None, report_markdown="")
    pe = out["valuation"]["peers"][0]
    names = {r["name"]: r["raw"] for r in pe["rows"]}
    assert names == {
        "RR Kabel": "55",
        "Polycab India": "46.69",
        "TCS": "15.29",
    }  # the correction borrows its name
    assert pe["subject"]["raw"] == "51.61"  # the upper band is the headline multiple
    assert [m["context"] for m in out["valuation"]["multiples"]] == ["cap price ₹272", "floor price ₹258"]


def test_ipo_block_and_plain_english_cite_every_figure():
    claims = [
        cl("price_band_upper", "272", "INR per share", "offer", stream="facts"),
        cl("price_band_lower", "258", "INR per share", "offer", stream="facts"),
        cl("lot_size", "55", "shares", "offer", stream="facts"),
        cl("lot_value", "1.496E+4", "INR", "cap price ₹272, 1 lot = 55 shares", stream="valuation"),
        cl("fresh_issue_amount", "3.2E+3", "INR million", "offer", stream="facts"),
        cl("ofs_amount", "2.32E+3", "INR million", "offer", stream="facts"),
        cl("objects_repayment", "1555", "INR million", "RHP", stream="major"),
        cl("general_corporate_purposes_cap", "8E+2", "INR million", "IPO 2026", stream="valuation"),
        cl("qib_subscription_combined", "0.0574", "x", "2026-09-28 17:00 IST", stream="demand"),
        cl("total_subscription_combined", "8.316", "x", "2026-09-28 17:00 IST", stream="demand"),
        cl("total_subscription_combined", "2.07", "x", "2026-09-25 17:07 IST", stream="demand"),
        cl("reservation_split", None, None, "IPO 2026", stream="valuation", claim_type="factual",
           statement="Retail portion is not less than 35% of the Offer; Non-Institutional (NII) portion is not less "
                     "than 15% of the Offer; QIB portion is not more than 50% of the Offer"),
        cl("listing_date", None, None, "2026-10-05", stream="major", claim_type="factual"),
    ]  # fmt: skip
    watch = {"open_date": "2026-09-25", "close_date": "2026-09-29", "listing_date": "2026-10-05",
             "snapshots": [{"as_of": "2026-09-29T17:00:00+05:30", "source": "nse_combined", "total_times": "92.27",
                            "categories": [{"code": "1", "times": "182.76"}, {"code": "3", "times": "30.55"},
                                           {"code": "1(a)", "times": None}]}]}  # fmt: skip
    out = build_insights(
        run_id=1, kind="ipo_report", claims=claims, synthesis=None, report_markdown="", watch=watch
    )
    ipo = out["ipo"]
    assert ipo["subscription_as_of"] == "2026-09-28 17:00 IST"  # the latest ledger snapshot, not day 1
    assert [(s["category"], s["display"]) for s in ipo["subscription"]] == [
        ("qib", "0.057x"),
        ("total", "8.32x"),
    ]
    assert {r["label"]: r["value"] for r in ipo["reservation"]} == {"QIB": 50, "NII": 15, "Retail": 35}
    gcp = next(p for p in ipo["proceeds"] if p["cap"])
    assert gcp["label"] == "General corporate purposes" and gcp["display"] == "₹800 Mn"
    assert [d["title"] for d in ipo["timeline"]] == [
        "Bidding opens",
        "Bidding closes",
        "Listing",
    ]  # cited beats monitor
    assert ipo["subscription_timeline"] == [{"as_of": "2026-09-29T17:00:00+05:30", "source": "nse_combined",
                                             "total": 92.27, "qib": 182.76, "retail": 30.55}]  # fmt: skip
    story = " ".join(out["plain_english"])
    assert "₹258–272" in story and "₹14,960" in story and "0.057x" in story
    for line in out["plain_english"]:
        assert "[C" in line  # nothing in the plain-English summary is uncited


def test_bond_block():
    claims = [
        cl("coupon_rate", "8.98", "%", "per annum", stream="facts"),
        cl("face_value", "1E+3", "INR", "per bond", stream="facts"),
        cl("last_price", "1076", "INR", "2026-09-28 16:00 IST", stream="facts"),
        cl("coupon_frequency", "12", "payments/year", "2029-03-13 maturity", stream="bond_terms"),
        cl("maturity_year", "2029", "year", "2029-03-13", stream="facts"),
        cl("gsec_yield_2y", "6.623", "%", "2026-09-28", stream="bond_pricing"),
        cl("rating", None, None, None, stream="bond_rating", claim_type="factual",
           statement="Series N7 carries AAA ratings from ICRA and CARE. More detail follows here."),
    ]  # fmt: skip
    out = build_insights(run_id=1, kind="bond_report", claims=claims, synthesis=None, report_markdown="")
    b = out["bond"]
    assert b["maturity"]["date"] == "2029-03-13" and b["frequency"]["display"] == "12 payments/yr"
    assert [y["label"] for y in b["yields"]] == ["This bond: coupon", "2-year G-sec"]
    assert b["ratings"][0]["text"] == "Series N7 carries AAA ratings from ICRA and CARE"
    assert any("above the ₹1,000" in s for s in out["plain_english"])


# --------------------------------------------------------------------------- report text and synthesis
RISKS_MD = """## Verdict box

| Item | View |
|---|---|
| **Verdict** | **HOLD** |

## Ranked risks

1. **Growth slowdown (high).**
   - CC growth was 2.4% [C1328].
   - Guidance cut (C1338).
2. **CEO transition (medium).** A reset is possible [C1419].
3. **Currency.** INR vs USD growth [C1497].

**Litigation summary**
- nothing here [C9]

## Bull vs bear
"""


def test_ranked_risks_keep_stated_severity_and_never_invent_one():
    risks = ranked_risks(RISKS_MD)
    assert [(r["rank"], r["title"], r["severity"]) for r in risks] == [
        (1, "Growth slowdown", "high"),
        (2, "CEO transition", "medium"),
        (3, "Currency", None),
    ]
    assert risks[0]["claim_ids"] == [1328, 1338] and "[C1338]" in risks[0]["text"]
    assert ranked_risks("## Snapshot\n\nno risks here") == []


def test_synthesis_parts_pass_through_with_citations():
    syn = {"verdict": "ACCUMULATE", "confidence": "medium", "horizon": "3-5 years",
           "entry_zone": "₹889–1,000 (C1626/C1627)", "condition": "Watch Q2 (C1440).",
           "reasons_for": [{"point": "Cheap at 13.5x", "weight": "high", "claim_ids": [1425]}],
           "reasons_against": [{"point": "Slow growth", "weight": "medium", "claim_ids": ["1328"]}],
           "scenarios": [{"name": "base", "horizon": "12m", "price_low": 933.28, "price_high": 1049.94,
                          "likelihood": "most likely", "rationale": "EPS +5% (C1635)"},
                         {"name": "bull", "horizon": "12m", "rationale": "no prices"}],
           "action_checklist": ["Buy in tranches (C1626/C1627)."]}  # fmt: skip
    claims = [cl("pe", "13.5", "x", "2026-09-28", stream="stock_valuation", id=1425),
              cl("x", "1", "x", "FY26", stream="stock_news", status="contradicted", id=1426)]  # fmt: skip
    out = build_insights(run_id=1, kind="stock_report", claims=claims, synthesis=syn,
                         report_markdown="# T\n\nCheap [C1425].")  # fmt: skip
    assert (
        out["verdict"]["word"] == "ACCUMULATE" and out["verdict"]["entry_zone"] == "₹889–1,000 [C1626][C1627]"
    )
    assert out["pros"] == [{"text": "Cheap at 13.5x", "weight": "high", "claim_ids": [1425]}]
    assert out["cons"][0]["claim_ids"] == [1328]
    assert len(out["scenarios"]) == 1 and out["scenarios"][0]["rationale"] == "EPS +5% [C1635]"
    assert out["checklist"] == [{"text": "Buy in tranches [C1626][C1627].", "claim_ids": [1626, 1627]}]
    q = out["quality"]
    assert q["total"] == 2 and q["by_status"]["contradicted"] == 1 and q["verified_pct"] == 50.0
    assert q["cited"] == 1 and q["cited_verified_pct"] == 100.0


def test_empty_run_yields_an_empty_but_complete_shape():
    out = build_insights(run_id=1, kind="bond_report", claims=[], synthesis=None, report_markdown=None)
    assert out["key_numbers"] == [] and out["financials"]["series"] == [] and out["risks"] == []
    assert out["quality"]["verified_pct"] is None and out["bond"]["yields"] == []


# --------------------------------------------------------------------------- the route, on a replayed real run
FIXTURE = Path(__file__).parent / "fixtures" / "eval" / "infosys-run9.json"


@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app

    with TestClient(create_app()) as c:
        yield c


def test_insights_route_on_replayed_infosys_run(client, env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim, ResearchRun
    from finresearch.evals.replay import import_run

    data = json.loads(FIXTURE.read_text())
    with session_scope() as s:
        run_id = import_run(s, data, slug_suffix="-" + tmp_path.name[-8:])
        s.get(ResearchRun, run_id).kind = "stock_report"
        status = {c.id: c.status for c in s.query(Claim).filter_by(run_id=run_id)}
    r = client.get(f"/api/runs/{run_id}/insights")
    assert r.status_code == 200
    out = r.json()
    assert out["kind"] == "stock_report" and out["verdict"]["word"] == "ACCUMULATE"
    rev = next(
        x
        for x in out["financials"]["series"]
        if x["key"] == "revenue" and x["unit"] == "₹ Cr" and x["freq"] == "annual"
    )
    assert [p["period"] for p in rev["points"]] == ["FY22", "FY23", "FY24", "FY25", "FY26"]
    every = [p for s in out["financials"]["series"] for p in s["points"]] + out["valuation"]["multiples"]
    assert every and all(status[p["claim_id"]] in ("verified", "needs_review") for p in every)
    assert all(status[p["claim_id"]] == p["status"] for p in every)
    assert [r["severity"] for r in out["risks"]][:2] == ["high", "high"]
    assert out["quality"]["total"] == len(data["claims"])
    assert client.get("/api/runs/999999999/insights").status_code == 404


def test_pack_entries_and_download(client, env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        co = get_or_create_company(s, f"ins-{tmp_path.name[-8:]}", "Ins Co")
        run = ResearchRun(company_id=co.id, kind="ipo_report", status="done", manifest={})
        s.add(run)
        s.flush()
        rid, slug = run.id, co.slug
    pack = env.reports_dir / slug / f"run-{rid}" / "06_Final_Report"
    pack.mkdir(parents=True)
    (pack / "report.md").write_text("# hi\n")
    body = client.get(f"/api/runs/{rid}/pack").json()
    assert body["files"] == ["06_Final_Report/report.md"]
    assert body["entries"] == [{"path": "06_Final_Report/report.md", "size": 5}]
    inline = client.get(f"/api/runs/{rid}/pack/06_Final_Report/report.md")
    assert "attachment" not in inline.headers.get("content-disposition", "")
    saved = client.get(f"/api/runs/{rid}/pack/06_Final_Report/report.md?download=1")
    assert saved.status_code == 200 and saved.headers["content-disposition"].startswith("attachment")


def test_ranked_risks_prefer_the_ranked_list_over_a_metrics_section():
    md = "## Risk\n\nVolatility 14.6% [C1].\n\n## Ranked risks\n\n1. **Cost (medium).** High TER [C2].\n"
    assert [(r["title"], r["severity"]) for r in ranked_risks(md)] == [("Cost", "medium")]


@pytest.mark.parametrize("name", ["synthesizer", "stock_synthesizer", "fund_synthesizer", "bond_synthesizer"])
def test_synthesizer_prompts_ask_for_plain_english_and_key_numbers(name):
    text = (
        Path(__file__).parents[1] / "src" / "finresearch" / "agents" / "prompts" / f"{name}.md"
    ).read_text()
    assert "Verdict box" in text and "## Key numbers" in text and "**Explained simply:**" in text
    assert text.index("Verdict box") < text.index(
        "## Key numbers"
    )  # the reader parses the first verdict table


def test_summary_headlines_use_verified_claims_only():
    claims = [
        cl("coupon_rate", "8.98", "%", "per annum", stream="facts"),
        cl(
            "ytm",
            "0.058255",
            "fraction",
            "2026-09-29 settlement",
            stream="bond_pricing",
            status="needs_review",
        ),
        cl(
            "benchmark_return_5y",
            "0.169",
            "fraction",
            "as of 2026-09-16",
            stream="bond_pricing",
            status="needs_review",
        ),
    ]
    out = build_insights(run_id=1, kind="bond_report", claims=claims, synthesis=None, report_markdown="")
    assert [t["label"] for t in out["key_numbers"]] == ["Coupon (a year)"]
    assert all(t["status"] == "verified" for t in out["key_numbers"])
    assert not any("5.83" in s for s in out["plain_english"])
    # the needs-review yield still reaches the charts, with its status
    assert any(y["status"] == "needs_review" and y["display"] == "5.83%" for y in out["bond"]["yields"])


def test_fair_values_pair_low_and_high_ends_but_never_invent_one():
    claims = [
        cl("entry_zone_low_12x", "888.84", "INR/share", "2026-09-28", stream="stock_valuation"),
        cl("entry_zone_high_13_5x", "999.95", "INR/share", "2026-09-28", stream="stock_valuation"),
        cl(
            "entry_zone_low",
            "668",
            "INR/share",
            "2026-09-28",
            stream="stock_valuation",
            status="needs_review",
        ),
        cl("fair_value_pe_band", "1222", "INR/share", "2026-09-28", stream="stock_valuation"),
        cl("fair_value_pb_band", "242.42", "INR per share", "high, FY2026 NAV-based", stream="valuation"),
    ]
    out = build_insights(run_id=1, kind="stock_report", claims=claims, synthesis=None, report_markdown="")
    fv = {(f["group"], f["role"]): (f["raw"], f["basis"]) for f in out["valuation"]["fair_value"]}
    assert fv == {
        ("entry_zone", "low"): ("888.84", "12x"),  # the verified low end beats the needs-review one
        ("entry_zone", "high"): ("999.95", "13.5x"),
        ("fair_value_pe_band", None): ("1222", None),  # one number stays a point
        ("fair_value_pb_band", "high"): ("242.42", None),  # only the high end is in the ledger
    }
