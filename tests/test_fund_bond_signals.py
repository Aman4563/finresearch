"""Fund and bond signals (docs/dev/RESEARCH_ROADMAP.md §D.3, §D.4): golden values for every formula, the FBIL and
AMFI TER adapters on recorded (trimmed) files, and both providers end to end on fixtures. Nothing touches the network."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from finresearch.adapters import amfi as amfi_mod
from finresearch.adapters import fbil as fbil_mod
from finresearch.adapters.amfi import AmfiClient, SchemeNav, parse_ter_xlsx, ter_key
from finresearch.adapters.fbil import FALLBACK_CURVE, FbilClient, ParCurve, ParPoint, parse_par_yield_xlsx
from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse_bonds import ListedBond, parse_live_bonds
from finresearch.fincalc import bonds as b
from finresearch.fincalc import credit as cr
from finresearch.fincalc import funds as F
from finresearch.signals import bond as bond_sig
from finresearch.signals import fund as fund_sig
from finresearch.signals import registry
from finresearch.suggest.profile import Profile

FIX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 9, 30)


# --------------------------------------------------------------------------- golden: statistics
def test_wilson_interval_matches_the_roadmap_example():
    lo, hi = F.wilson_interval(7, 10)
    assert lo == pytest.approx(0.3968, abs=1e-4) and hi == pytest.approx(
        0.8922, abs=1e-4
    )  # §C.10: "about 40-89 %"
    lo, hi = F.wilson_interval(0, 10)
    assert lo == 0 and hi == pytest.approx(0.2775, abs=1e-4)
    lo, hi = F.wilson_interval(2.25, 2.25)  # fractional n (effective windows)
    assert hi == 1 and lo == pytest.approx(0.3693, abs=1e-4)
    with pytest.raises(ValueError):
        F.wilson_interval(3, 2)


def test_effective_windows_percentile_capture_and_r_squared():
    assert F.effective_windows(13, 91.3, 3 * 365.25) == pytest.approx(1 + 12 * 91.3 / 1095.75)  # ~2.0
    assert F.effective_windows(5, 400, 365) == 5 and F.effective_windows(0, 91, 365) == 0
    assert F.percentile_rank([1, 2, 3, 4], 3) == 0.625 and F.percentile_rank([1, 2], 5) == 1.0
    assert F.median([3, 1, 2]) == 2 and F.median([4, 1, 2, 3]) == 2.5
    assert F.downside_capture([-0.05, 0.02, -0.10], [-0.10, 0.03, -0.10]) == pytest.approx(0.75)
    assert F.downside_capture([0.1], [0.2]) is None
    assert F.r_squared([1, 2, 3, 4, 5], [2, 4, 5, 4, 5]) == pytest.approx(0.6)  # r = 0.7746
    assert (
        F.r_squared([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0) and F.r_squared([1, 2, 3], [5, 5, 5]) is None
    )


# --------------------------------------------------------------------------- golden: yields and credit
def test_effective_annual_conversions():
    assert b.effective_annual("0.0666", 2) == Decimal("0.06770889")  # FBIL publishes 6.77 annualised
    assert b.effective_annual("0.064", 4) == pytest.approx(Decimal("0.0655524"), abs=Decimal("1e-7"))
    assert b.effective_annual("0.09", 1) == Decimal("0.09")
    # a 6.66 % half-yearly G-sec after 31.2 % tax: 6.66 x 0.688 = 4.58208 % half-yearly -> 4.63457 % a year
    assert b.after_tax_par_yield("0.0666", "0.312", 2) == pytest.approx(
        Decimal("0.0463457"), abs=Decimal("1e-7")
    )


def test_after_tax_ytm_counts_the_accrued_interest_the_buyer_pays():
    s, m, c, f = date(2026, 9, 30), date(2029, 3, 13), Decimal("0.0898"), Decimal(1000)
    ai = b.accrued_interest(s, m, c, 1, f)
    clean = Decimal(1076) - ai  # NSE's dirty price
    assert round(ai, 4) == Decimal("49.4515")
    wrong = b.after_tax_ytm(clean, s, m, c, 1, "0.312", face=f)
    right = b.after_tax_ytm(clean, s, m, c, 1, "0.312", face=f, accrued=ai)
    assert wrong == Decimal("0.06496204") and right == Decimal("0.04321165")
    assert right < b.ytm(clean, s, m, c, 1, f) * Decimal(
        "0.688"
    )  # a premium bond keeps less than YTM x (1-t)
    # on a coupon date nothing has accrued and the two agree
    on = date(2027, 3, 13)
    assert b.accrued_interest(on, m, c, 1) == 0
    assert b.after_tax_ytm(
        100, on, m, c, 1, "0.3", accrued=b.accrued_interest(on, m, c, 1)
    ) == b.after_tax_ytm(100, on, m, c, 1, "0.3")


def test_credit_grades_default_rates_and_expected_loss():
    cases = {"AA/Stable, AA+/Negative": "AA", "[ICRA]AA+": "AA", "CRISIL AAA/stable/": "AAA", "A-/Stable": "A",
             "IND AA(CE)": "AA", "BBB-": "BBB", "SOV": "SOV", "CARE D": "D", "CRISIL A1+": None, None: None,
             "AAA/Stable, AA-/Stable": "AA"}  # fmt: skip
    for text, grade in cases.items():
        assert cr.base_grade(text) == grade, text
    # roadmap §D.4: BBB, 3-year CDR 1.97 % -> about 0.66 %/yr x 0.6 = about 0.40 pp a year
    assert cr.annual_pd("BBB") == pytest.approx(Decimal("0.0066103"), abs=Decimal("1e-7"))
    assert cr.expected_loss("BBB") == pytest.approx(Decimal("0.0039662"), abs=Decimal("1e-7"))
    assert cr.expected_loss("AAA") == 0 and cr.expected_loss("AA", "0.4") == pytest.approx(
        Decimal("0.4") * (1 - Decimal(str((1 - 0.0014) ** (1 / 3)))), abs=Decimal("1e-9")
    )
    assert cr.cumulative_default_rate("A", 2.5) == Decimal("0.00455")  # halfway between 0.33 % and 0.58 %
    assert cr.cumulative_default_rate("A", 0.5) == Decimal("0.00035")
    assert cr.cumulative_default_rate("AA", 3) == Decimal("0.0014")
    assert cr.cumulative_default_rate("AA", 5) == pytest.approx(Decimal(str(1 - (1 - 0.0014) ** (5 / 3))),
                                                             abs=Decimal("1e-9"))  # fmt: skip
    assert cr.cumulative_default_rate("SOV", 10) == 0 and cr.cumulative_default_rate("B", 1) is None
    assert cr.next_lower("AA") == "A" and cr.next_lower("BB") is None
    with pytest.raises(ValueError):
        cr.expected_loss("A", "1.5")


def test_holding_period_yield_at_an_unchanged_yield_is_the_coupon():
    s, m, face, c = date(2026, 9, 30), date(2029, 9, 30), Decimal(1000), Decimal("0.09")
    exit_day = date(2027, 9, 30)
    untaxed = bond_sig.holding_period_yield(Decimal(1000), s, exit_day, 0.09, m, c, 1, face, Decimal(0), Decimal(0),
                                            Decimal(1000))  # fmt: skip
    taxed = bond_sig.holding_period_yield(Decimal(1000), s, exit_day, 0.09, m, c, 1, face, Decimal("0.312"),
                                          Decimal(0), Decimal(1000))  # fmt: skip
    assert float(untaxed) == pytest.approx(0.09, abs=1e-6) and float(taxed) == pytest.approx(
        0.06192, abs=1e-6
    )
    worse = bond_sig.holding_period_yield(Decimal(1000), s, exit_day, 0.10, m, c, 1, face, Decimal(0), Decimal(0),
                                          Decimal(1000))  # fmt: skip
    assert worse < untaxed  # yields rose: the sale price fell


# --------------------------------------------------------------------------- adapters on recorded files
def test_fbil_par_yield_sheet_parses_and_interpolates():
    curve = parse_par_yield_xlsx((FIX / "fbil" / "gsec_20260923_trimmed.xlsx").read_bytes())
    assert curve.as_of == date(2026, 9, 23) and len(curve.points) == 12
    assert curve.points[4] == ParPoint(Decimal(3), Decimal("0.0666"), Decimal("0.0677"))
    assert curve.par_yield(3.125) == Decimal("0.06680000")  # halfway between 6.66 % (3y) and 6.70 % (3.25y)
    assert curve.par_yield(0.1) == Decimal("0.0534") and curve.par_yield(60) == Decimal(
        "0.0764"
    )  # flat beyond
    # FBIL's own annualised column is the half-yearly rate made effective annual
    for p in curve.points:
        assert float(b.effective_annual(p.semi_annual, 2)) == pytest.approx(float(p.annualised), abs=6e-4)
    assert FALLBACK_CURVE.fallback and FALLBACK_CURVE.par_yield(3) == Decimal("0.0666")


@respx.mock
async def test_fbil_client_reads_the_latest_published_curve(tmp_path):
    respx.get(fbil_mod.LATEST_URL).mock(return_value=httpx.Response(200, json=[
        {"processRunDate": "2026-09-22", "archive": "x"}, {"processRunDate": "2026-09-23", "archive": "x"}]))  # fmt: skip
    dl = respx.get(fbil_mod.DOWNLOAD_URL).mock(
        return_value=httpx.Response(200, content=(FIX / "fbil" / "gsec_20260923_trimmed.xlsx").read_bytes())
    )
    async with FbilClient(PoliteClient(cache_dir=tmp_path)) as fb:
        curve = await fb.latest_par_curve()
    assert curve.as_of == date(2026, 9, 23) and not curve.fallback and "date=2026-09-23" in curve.source
    assert dl.calls.last.request.url.params["date"] == "2026-09-23"


def test_ter_file_keeps_the_latest_row_per_scheme():
    t = parse_ter_xlsx((FIX / "amfi" / "ter_sep2026_trimmed.xlsx").read_bytes())
    pp = t[ter_key("Parag Parikh Flexi Cap Fund")]
    assert pp.day == date(2026, 9, 29) and pp.direct == Decimal("0.69") and pp.regular == Decimal("1.31")
    assert pp.category == "Equity Scheme - Flexi Cap Fund"
    assert t[ter_key("HDFC Nifty 50 Index Fund")].direct == Decimal("0.29")
    assert ter_key("HDFC  Large-Cap Fund") == "hdfc large cap fund"


@respx.mock
async def test_amfi_ter_download_asks_for_the_month_as_excel(tmp_path):
    route = respx.get(amfi_mod.TER_URL).mock(
        return_value=httpx.Response(200, content=(FIX / "amfi" / "ter_sep2026_trimmed.xlsx").read_bytes())
    )
    async with AmfiClient(PoliteClient(cache_dir=tmp_path)) as a:
        t = await a.ter(date(2026, 9, 30))
    q = route.calls.last.request.url.params
    assert q["Month"] == "09-2026" and q["excel"] == "true" and q["MF_ID"] == "All" and len(t) == 5


# --------------------------------------------------------------------------- fund: pure pieces
def test_quarter_ends_move_back_to_weekdays():
    assert fund_sig.quarter_ends(date(2023, 1, 1), date(2024, 12, 31)) == [
        date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 29), date(2023, 12, 29),  # 30 Sep, 31 Dec 2023: weekend
        date(2024, 3, 29), date(2024, 6, 28), date(2024, 9, 30), date(2024, 12, 31)]  # fmt: skip


def test_category_needs_and_passive_detection():
    assert fund_sig.category_needs("Equity Scheme - Small Cap Fund") == ("long", "high")
    assert fund_sig.category_needs("Equity Scheme - Large Cap Fund") == ("long", "medium")
    assert fund_sig.category_needs("Debt Scheme - Liquid Fund") == ("short", "low")
    assert fund_sig.category_needs("Hybrid Scheme - Balanced Advantage") == ("medium", "medium")
    assert fund_sig.category_needs("Something New") is None
    assert fund_sig.is_passive("Other Scheme - Index Funds") and not fund_sig.is_passive(
        "Equity Scheme - Mid Cap Fund"
    )


def scheme(code: str, name: str, category="Equity Scheme - Mid Cap Fund", plan="Direct Plan", option="Growth",
           nav="100", day=date(2026, 9, 29)) -> SchemeNav:  # fmt: skip
    return SchemeNav(code, name, plan, option, None, None, Decimal(nav), day, category, "Test MF")


def synthetic_snapshots(anchors: list[date], growth: dict[str, float]) -> list[dict]:
    """Every scheme compounding at its own yearly rate from 100 at the first anchor."""
    t0 = anchors[0]
    return [{c: (a, Decimal(str(round(100 * (1 + g) ** ((a - t0).days / 365.25), 6)))) for c, g in growth.items()}
            for a in anchors]  # fmt: skip


def test_windows_rank_the_fund_among_its_peers():
    anchors = fund_sig.quarter_ends(date(2019, 9, 1), date(2026, 6, 30))
    growth = {"ME": 0.13, "P1": 0.08, "P2": 0.10, "P3": 0.12, "P4": 0.14, "P5": 0.16}
    snaps = synthetic_snapshots(anchors, growth)
    ws = fund_sig.windows("ME", ["P1", "P2", "P3", "P4", "P5"], anchors, snaps, 3)
    assert len(ws) == len(anchors) - 12
    w = ws[-1]
    assert w.median == pytest.approx(0.12, abs=1e-4) and w.fund == pytest.approx(0.13, abs=1e-4)
    assert w.percentile == 0.6 and w.peers == 5  # beats 3 of 5
    q = fund_sig.quarterly("ME", ["P1", "P2", "P3", "P4", "P5"], anchors, snaps)
    assert q["down_quarters"] == 0 and q["downside_capture"] is None and q["quarters"] == len(anchors) - 1
    # a peer without a NAV near an anchor (not yet launched) drops out of that window, and with fewer than
    # MIN_PEERS (5) left the window is skipped
    snaps[0].pop("P5")
    assert fund_sig.windows("ME", ["P1", "P2", "P3", "P4", "P5"], anchors, snaps, 3)[0].end == anchors[13]


def analysis(hit: int, n: int, *, direct=True, ter_pct=0.2, passive=False, category="Equity Scheme - Mid Cap Fund",
             r2=0.9) -> dict:  # fmt: skip
    ends = [date(2023, 3, 31) + timedelta(days=91 * i) for i in range(n)]
    ws = [{"end": e.isoformat(), "years": 3, "fund": 0.15 if i < hit else 0.10, "median": 0.12, "p25": 0.1,
           "p75": 0.14, "percentile": 0.7 if i < hit else 0.3, "peers": 20} for i, e in enumerate(ends)]  # fmt: skip
    return {"scheme": {"scheme_code": "999", "name": "Test Midcap Fund", "plan": "Direct Plan" if direct else "Regular",
                       "option": "Growth", "category": category, "amc": "Test MF", "nav_date": "2026-09-29"},
            "direct": direct, "growth": True, "passive": passive, "peers": 20, "anchors": [],
            "windows_3y": ws, "windows_1y": ws,
            "quarterly": {"quarters": 20, "down_quarters": 5, "downside_capture": 0.8, "r_squared": r2,
                          "drawdown": -0.2, "category_drawdown": -0.25},
            "ter": {"available": True, "matched": True, "ter": 0.6 if direct else 1.8, "direct_ter": 0.6,
                    "regular_ter": 1.8, "day": "2026-09-29", "percentile": ter_pct, "peers": 20,
                    "category_median": 0.9},
            "ter_error": None,
            "index_alternatives": [{"scheme_code": "1", "name": "Cheap Nifty Midcap 150 Index Fund", "direct_ter": 0.2}],
            "as_of": "2026-09-30T06:00:00+00:00", "sources": []}  # fmt: skip


LONG_MEDIUM_HIGH = Profile(horizon="long", risk_appetite="high")


def test_fund_buy_when_cheap_consistent_and_suitable():
    s = fund_sig.build_signal(analysis(10, 13), LONG_MEDIUM_HIGH)
    assert s.action == "BUY" and s.validation.status == "base_rate" and s.validation.n == 13
    n_eff = F.effective_windows(13, fund_sig.QUARTER_DAYS, 3 * 365.25)
    lo, hi = F.wilson_interval(10 / 13 * n_eff, n_eff)
    assert s.probability_interval == (round(lo, 4), round(hi, 4))
    z2 = 1.96**2
    assert s.probability == round(
        (10 / 13 + z2 / (2 * n_eff)) / (1 + z2 / n_eff), 4
    )  # Wilson centre, pulled to 50 %
    assert s.base_rate["p"] == round(10 / 13, 4)
    assert s.score == pytest.approx(sum(f.contribution for f in s.factors))  # the factors add up to the score


def test_fund_switch_review_suggests_an_index_fund_when_expensive_and_lagging():
    s = fund_sig.build_signal(analysis(3, 13, ter_pct=0.9), LONG_MEDIUM_HIGH)
    assert s.action == "REDUCE"
    assert any("index fund in the same category" in c and "Cheap Nifty Midcap 150" in c for c in s.caveats)
    assert any("exit load" in c for c in s.caveats)


def test_fund_regular_plan_costs_points_and_says_so():
    s = fund_sig.build_signal(analysis(10, 13, direct=False, ter_pct=0.3), LONG_MEDIUM_HIGH)
    reg = next(f for f in s.factors if f.name == "Regular plan")
    assert reg.value == 1.2 and reg.contribution == -15 and s.action != "BUY"
    assert any("direct plan" in c for c in s.caveats)


def test_fund_avoid_when_the_category_does_not_fit_the_profile():
    s = fund_sig.build_signal(analysis(10, 13), Profile(horizon="short", risk_appetite="low"))
    assert s.action == "AVOID" and next(f for f in s.factors if f.name.startswith("Fits")).contribution == -40
    # ctx overrides the profile
    assert fund_sig.build_signal(analysis(10, 13), Profile(horizon="short"), {"horizon": "long", "risk": "high"}
                                 ).action == "BUY"  # fmt: skip


def test_fund_style_drift_and_missing_ter_block_buy():
    assert fund_sig.build_signal(analysis(10, 13, r2=0.3), LONG_MEDIUM_HIGH).action == "HOLD"
    a = analysis(10, 13)
    a["ter"], a["ter_error"] = {"available": False}, "AMFI TER file unavailable: HTTP 502"
    s = fund_sig.build_signal(a, LONG_MEDIUM_HIGH)
    assert (
        s.action == "ACCUMULATE"
        and "HTTP 502" in next(f for f in s.factors if f.name.startswith("Expense")).explanation
    )


def test_fund_no_signal_for_passive_funds_and_short_histories():
    p = fund_sig.build_signal(
        analysis(10, 13, passive=True, category="Other Scheme - Index Funds"), LONG_MEDIUM_HIGH
    )
    assert p.action == "NO_SIGNAL" and p.probability is None and any("Index fund" in c for c in p.caveats)
    short = fund_sig.build_signal(analysis(2, 3), LONG_MEDIUM_HIGH)
    assert short.action == "NO_SIGNAL" and "Not enough history" in short.caveats[-1]


# --------------------------------------------------------------------------- fund: provider end to end
@pytest.fixture
def fund_sources(monkeypatch, tmp_path):
    rows = [scheme("1001", "Alpha Midcap Fund"), scheme("1002", "Alpha Midcap Fund", plan="Regular Plan"),
            *[scheme(str(2000 + i), f"Peer {i} Midcap Fund") for i in range(6)],
            scheme("3001", "Cheap Nifty Midcap 150 Index Fund", category="Other Scheme - Index Funds")]  # fmt: skip
    growth = {"1001": 0.15, "1002": 0.14, **{str(2000 + i): 0.08 + 0.01 * i for i in range(6)}}
    anchors = fund_sig.quarter_ends(date(2019, 9, 29), date(2026, 9, 28))
    snaps = dict(zip(anchors, synthetic_snapshots(anchors, growth), strict=True))
    calls = []

    async def schemes():
        return rows

    async def snapshot(day):
        calls.append(day)
        return snaps[day]

    async def ter(month):
        return parse_ter_xlsx((FIX / "amfi" / "ter_sep2026_trimmed.xlsx").read_bytes())

    async def profile():
        return LONG_MEDIUM_HIGH

    monkeypatch.setattr(
        fund_sig, "SOURCES", fund_sig.FundSources(schemes, snapshot, ter, profile, lambda: TODAY)
    )
    monkeypatch.setattr(fund_sig, "_CACHE", fund_sig.TtlCache())
    return calls


def test_fund_provider_and_consistency_route(fund_sources, monkeypatch):
    from finresearch.api import create_app

    monkeypatch.setattr(registry, "_PROVIDERS", {"fund": fund_sig.fund_signal, "bond": bond_sig.bond_signal})
    monkeypatch.setattr(registry, "_loaded", True)
    with TestClient(create_app()) as c:
        r = c.get("/api/signals/fund/1001")
        assert r.status_code == 200, r.text
        s = r.json()
        assert (
            s["asset"] == "fund"
            and s["action"] in ("ACCUMULATE", "BUY")
            and s["validation"]["status"] == "base_rate"
        )
        assert (
            s["base_rate"]["p"] == 1.0 and s["probability"] < 1
        )  # 15 % beats every peer, but the estimate shrinks
        ter = next(f for f in s["factors"] if f["name"] == "Expense ratio vs category")
        assert (
            ter["value"] is None and "no row" in ter["explanation"]
        )  # synthetic names aren't in the TER file
        cons = c.get("/api/funds/1001/consistency").json()
        assert (
            cons["windows_3y"][-1]["percentile"] == 1.0 and cons["peers"] == 6
        )  # the regular twin isn't a peer
        n_calls = len(fund_sources)
        c.get("/api/funds/1001/consistency")
        assert len(fund_sources) == n_calls  # cached
        assert c.get("/api/signals/fund/9999").status_code == 404
        assert c.get("/api/signals/fund/abc").status_code == 422
        reg = c.get(
            "/api/signals/fund/1002"
        ).json()  # the regular plan: compared with regular peers (none here)
        assert reg["action"] == "NO_SIGNAL" and any(f["name"] == "Regular plan" for f in reg["factors"])
        idx = c.get("/api/signals/fund/3001").json()
        assert idx["action"] == "NO_SIGNAL"
        assets = {a["asset"]: a["available"] for a in c.get("/api/signals").json()["assets"]}
        assert assets["fund"] and assets["bond"]


async def test_snapshot_is_cached_on_disk_for_past_anchors(monkeypatch):
    anchor = date(2026, 6, 30)
    rows = [
        scheme(str(i), f"S{i}", nav=str(10 + i % 7), day=anchor - timedelta(days=i % 2)) for i in range(2500)
    ]
    seen = []

    async def history(self, start, end, amc_code=None):
        seen.append((start, end))
        return rows

    monkeypatch.setattr(AmfiClient, "history", history)
    monkeypatch.setattr(fund_sig, "SOURCES", fund_sig.FundSources(today=lambda: TODAY))
    first = await fund_sig._snapshot(anchor)
    again = await fund_sig._snapshot(anchor)
    assert first == again and len(seen) == 1 and first["3"] == (date(2026, 6, 29), Decimal(13))
    assert fund_sig._snapshot_path(anchor).exists()
    # a holiday: too few rows for the day, so a wider window is asked for
    seen.clear()
    rows[:] = rows[:10]
    await fund_sig._snapshot(date(2026, 3, 31))
    assert seen == [(date(2026, 3, 31), date(2026, 3, 31)), (date(2026, 3, 25), date(2026, 3, 31))]


# --------------------------------------------------------------------------- bond: pure assessment
FLAT_7 = ParCurve(as_of=date(2026, 9, 29), points=[ParPoint(Decimal(1), Decimal("0.07"), None),
                                                  ParPoint(Decimal(10), Decimal("0.07"), None)])  # fmt: skip


def par_bond(**kw) -> ListedBond:
    base = dict(symbol="TEST9", series="N1", isin="INE000A07001", coupon_pct=Decimal(9), face_value=Decimal(1000),
                last_price=Decimal(1000), close=Decimal(1000), maturity=date(2029, 9, 30), next_interest_date=None,
                rating="CRISIL AA/Stable", rating_agency="CRISIL", traded_value=Decimal(500000), as_of=None)  # fmt: skip
    base.update(kw)
    return ListedBond(**base)


def factor(s, name):
    return next(f for f in s.factors if f.name == name)


def test_bond_golden_par_bond_against_a_flat_curve():
    """A 9 % annual-pay AA bond at par on a coupon date, 30 % slab (31.2 % with cess), 3 years to maturity."""
    s = bond_sig.assess(par_bond(), FLAT_7, Profile(tax_slab_pct=30), {"freq": "1"}, TODAY, None)
    assert factor(s, "Pre-tax YTM").value == pytest.approx(9.0, abs=1e-3)
    assert factor(s, "Post-tax YTM").value == pytest.approx(6.192, abs=1e-3)  # 9 x 0.688, at par
    el = 0.6 * (1 - (1 - 0.0014) ** (1 / 3)) * 100  # 0.02803 pp
    assert factor(s, "Expected credit loss").value == pytest.approx(el, abs=1e-3)
    assert factor(s, "G-sec par yield").value == pytest.approx(7.1225, abs=1e-3)  # 7 % half-yearly
    gsec_post = ((1 + 0.07 * 0.688 / 2) ** 2 - 1) * 100  # 4.8740 %
    assert factor(s, "G-sec after tax").value == pytest.approx(gsec_post, abs=1e-3)
    fd_post = ((1 + 0.063 * 0.688 / 4) ** 4 - 1) * 100  # SBI 3-5 years 6.30 %, quarterly: 4.4054 %
    assert factor(s, "FD after tax").value == pytest.approx(fd_post, abs=1e-3)
    spread = 6.192 - el - gsec_post  # 1.29 pp: short of the 1.5 pp asked of AA
    assert factor(s, "Spread over G-sec (after tax and loss)").value == pytest.approx(spread, abs=2e-3)
    assert s.action == "HOLD"
    assert s.probability == pytest.approx(
        1 - float(cr.cumulative_default_rate("AA", 1096 / 365.25)), abs=1e-4
    )
    assert s.probability_interval[0] == pytest.approx(1 - float(cr.cumulative_default_rate("A", 1096 / 365.25)),
                                                      abs=1e-4)  # fmt: skip
    assert s.score == pytest.approx(sum(f.contribution for f in s.factors), abs=0.2)
    # asking for a 1 pp credit spread instead makes it a buy
    assert bond_sig.assess(par_bond(), FLAT_7, Profile(tax_slab_pct=30), {"freq": "1", "min_spread": "1"}, TODAY,
                           None).action == "BUY"  # fmt: skip


def test_bond_avoid_when_it_does_not_beat_the_risk_free_alternative():
    s = bond_sig.assess(par_bond(coupon_pct=Decimal("6.5")), FLAT_7, Profile(tax_slab_pct=30), {"freq": "1"}, TODAY,
                        None)  # fmt: skip
    assert s.action == "AVOID" and s.probability == 0 and any("doesn't beat" in c for c in s.caveats)


def test_bond_never_shows_a_zero_width_range():
    """Audit follow-up: an AVOID bond showed "0 %, range 0 %–0 %". It fails by arithmetic (no default needed), so the
    0 % is a rule's certainty: no range, rule-based. A BB bond (no worse grade in CRISIL's table) had equal ends too."""
    avoid = bond_sig.assess(par_bond(coupon_pct=Decimal("6.5")), FLAT_7, Profile(tax_slab_pct=30), {"freq": "1"},
                            TODAY, None)  # fmt: skip
    assert avoid.probability == 0 and avoid.probability_interval is None
    assert avoid.validation.status == "rule_based" and "arithmetic" in avoid.validation.description
    assert avoid.to_json()["probability_interval"] is None
    bb = bond_sig.assess(par_bond(coupon_pct=Decimal(16), rating="CRISIL BB/Stable"), FLAT_7,
                         Profile(tax_slab_pct=30), {"freq": "1"}, TODAY, None)  # fmt: skip
    # CRISIL BB 3-year CDR 9.70 %, extended at a constant annual rate to 1,096 days: 1 - 0.903 ** (3.0007 / 3)
    assert bb.probability == pytest.approx(0.903 ** ((1096 / 365.25) / 3), abs=1e-4)
    assert bb.probability_interval is None and bb.validation.status == "base_rate"
    assert any(c.startswith("No range") for c in bb.caveats)
    # an AA bond still has its downgrade range (A's CDR is higher): unchanged
    aa = bond_sig.assess(par_bond(), FLAT_7, Profile(tax_slab_pct=30), {"freq": "1"}, TODAY, None)
    assert aa.probability_interval[0] < aa.probability_interval[1]


def test_bond_tax_free_and_ctx_overrides():
    s = bond_sig.assess(par_bond(coupon_pct=Decimal("6.5"), rating=None), FLAT_7, Profile(tax_slab_pct=30),
                        {"freq": "1", "tax_free": "1", "rating": "AAA", "fd": "7.5"}, TODAY, None)  # fmt: skip
    assert factor(s, "Post-tax YTM").value == pytest.approx(6.5, abs=1e-3)  # nothing taxed at par
    assert factor(s, "FD rate").value == pytest.approx(((1 + 0.075 / 4) ** 4 - 1) * 100, abs=1e-3)
    assert s.action in ("BUY", "HOLD") and factor(s, "Expected credit loss").value == 0


def test_bond_no_signal_paths():
    p = Profile(tax_slab_pct=30)
    unrated = bond_sig.assess(par_bond(rating=None), FLAT_7, p, {}, TODAY, None)
    assert unrated.action == "NO_SIGNAL" and "rating" in unrated.caveats[0]
    assert any(f.name == "Pre-tax YTM" for f in unrated.factors)  # the yields are still shown
    junk = bond_sig.assess(par_bond(rating="CARE B+"), FLAT_7, p, {}, TODAY, None)
    assert junk.action == "NO_SIGNAL" and "below BB" in junk.caveats[0]
    partly = bond_sig.assess(par_bond(face_value=Decimal(800)), FLAT_7, p, {}, TODAY, None)
    assert partly.action == "NO_SIGNAL" and "partly redeemed" in partly.caveats[0]
    matured = bond_sig.assess(par_bond(maturity=date(2026, 9, 1)), FLAT_7, p, {}, TODAY, None)
    assert matured.action == "NO_SIGNAL"
    with pytest.raises(ValueError):
        bond_sig.assess(par_bond(), FLAT_7, p, {"freq": "3"}, TODAY, None)
    # a price no yield in -50 %..100 % explains (a stale or mis-scaled quote): no signal, not a 100 % "YTM"
    absurd = bond_sig.assess(par_bond(last_price=Decimal(10)), FLAT_7, p, {"freq": "1"}, TODAY, None)
    assert absurd.action == "NO_SIGNAL" and "No yield fits" in absurd.caveats[0]


def test_bond_sold_early_uses_rate_scenarios():
    s = bond_sig.assess(par_bond(coupon_pct=Decimal(10)), FLAT_7, Profile(tax_slab_pct=30),
                        {"freq": "1", "horizon_years": "1"}, TODAY, None)  # fmt: skip
    assert s.horizon.startswith("1 years") and "sold after 1 years" in s.event
    lo, hi = s.probability_interval
    assert lo <= s.probability <= hi and factor(s, "Rate sensitivity").contribution < 0
    assert any("1 pp higher" in c for c in s.caveats)


def test_bond_uses_the_verified_frequency_unless_chosen():
    s = bond_sig.assess(par_bond(), FLAT_7, Profile(tax_slab_pct=30), {}, TODAY, (2, {"run_id": 12}))
    assert "research run #12" in factor(s, "Pre-tax YTM").explanation
    assumed = bond_sig.assess(par_bond(), FLAT_7, Profile(tax_slab_pct=30), {}, TODAY, None)
    assert any("assumed yearly" in c for c in assumed.caveats)


def test_fd_table_by_tenor():
    assert bond_sig.fd_rate_for(0.5) == Decimal("0.059") and bond_sig.fd_rate_for(2.5) == Decimal("0.064")
    assert bond_sig.fd_rate_for(4) == Decimal("0.063") and bond_sig.fd_rate_for(30) == Decimal("0.0605")


# --------------------------------------------------------------------------- bond: provider end to end
def test_bond_provider_on_the_recorded_nse_list(monkeypatch):
    from finresearch.api import create_app

    listed = parse_live_bonds(json.loads((FIX / "nse" / "bonds_live_trimmed.json").read_text()))

    async def bonds():
        return listed

    async def curve():
        return parse_par_yield_xlsx((FIX / "fbil" / "gsec_20260923_trimmed.xlsx").read_bytes())

    async def verified(isin):
        return None

    async def profile():
        return Profile(tax_slab_pct=30)

    monkeypatch.setattr(bond_sig, "SOURCES", bond_sig.BondSources(bonds, curve, verified, profile,
                                                                  lambda: date(2026, 9, 28)))  # fmt: skip
    monkeypatch.setattr(registry, "_PROVIDERS", {"fund": fund_sig.fund_signal, "bond": bond_sig.bond_signal})
    monkeypatch.setattr(registry, "_loaded", True)
    with TestClient(create_app()) as c:
        s = c.get("/api/signals/bond/INE413U07475", params={"freq": "1", "tax_slab_pct": "30"}).json()
        assert s["asset"] == "bond" and s["action"] in ("BUY", "HOLD", "AVOID")
        names = [f["name"] for f in s["factors"]]
        assert all(n in names for n in bond_sig.LADDER)
        assert s["validation"]["status"] == "base_rate" and 0.99 < s["probability"] <= 1
        assert "fbil.org.in" in " ".join(s["sources"])
        assert (
            c.get("/api/signals/bond/INE906B07DF8").json()["action"] == "NO_SIGNAL"
        )  # unrated in NSE's list
        assert (
            c.get("/api/signals/bond/INE148I07RB1").json()["action"] == "NO_SIGNAL"
        )  # face 800: partly redeemed
        assert c.get("/api/signals/bond/NOTANISIN").status_code == 422
        assert c.get("/api/signals/bond/INE000A07999").status_code == 404


def test_registry_finds_the_fund_and_bond_providers():
    have = registry.providers()
    assert have["fund"] is fund_sig.fund_signal and have["bond"] is bond_sig.bond_signal


def test_fund_fit_is_not_judged_on_the_ipo_listing_horizon():
    s = fund_sig.build_signal(analysis(10, 13), Profile(horizon="listing", risk_appetite="high"))
    fit = next(f for f in s.factors if f.name.startswith("Fits"))
    assert fit.value is None and fit.contribution == 0 and "IPO listings" in fit.explanation
    assert s.action == "ACCUMULATE"  # BUY needs a known fit


def test_bond_signal_carries_the_nse_list_warnings():
    s = bond_sig.assess(par_bond(next_interest_date=date(2026, 7, 30), as_of=None), FLAT_7, Profile(tax_slab_pct=30),
                        {"freq": "1"}, TODAY, None)  # fmt: skip
    assert not any("next-interest" in c for c in s.caveats)  # no as_of: NSE's warning can't fire
    from datetime import datetime

    s = bond_sig.assess(par_bond(next_interest_date=date(2026, 7, 30), as_of=datetime(2026, 9, 30, 12)), FLAT_7,
                        Profile(tax_slab_pct=30), {"freq": "1"}, TODAY, None)  # fmt: skip
    assert any("next-interest date 2026-07-30 is in the past" in c for c in s.caveats)


def test_bond_verified_frequency_is_used_and_an_assumed_one_lowers_confidence():
    """Regression (coordinator review of #104): the L&T NCD INE027E07998 pays monthly per a verified claim; the
    signal must use that, not yearly."""
    listed = parse_live_bonds(json.loads((FIX / "nse" / "bonds_live_trimmed.json").read_text()))
    ltf = max((x for x in listed if x.isin == "INE027E07998"), key=lambda x: x.traded_value or 0)
    curve = parse_par_yield_xlsx((FIX / "fbil" / "gsec_20260923_trimmed.xlsx").read_bytes())
    p, day = Profile(tax_slab_pct=30), date(2026, 9, 28)
    monthly = bond_sig.assess(
        ltf, curve, p, {}, day, (12, {"kind": "verified", "claim_id": 2180, "run_id": 12})
    )
    fq = factor(monthly, "Coupon frequency")
    assert fq.value == "monthly (verified)" and fq.contribution == 0 and fq.source == "claim:2180"
    assert "research run #12" in factor(monthly, "Pre-tax YTM").explanation
    assert not any("assumed yearly" in c for c in monthly.caveats)
    ai = b.accrued_interest(day, ltf.maturity, ltf.coupon_pct / 100, 12, ltf.face_value)
    clean = ltf.last_price - ai
    expected = b.effective_annual(b.after_tax_ytm(clean, day, ltf.maturity, ltf.coupon_pct / 100, 12, Decimal("0.312"),
                                                  capital_gains_rate=Decimal("0.13"), face=ltf.face_value,
                                                  accrued=ai), 12)  # fmt: skip
    assert factor(monthly, "Post-tax YTM").value == round(float(expected) * 100, 3)

    assumed = bond_sig.assess(ltf, curve, p, {}, day, None)
    fa = factor(assumed, "Coupon frequency")
    assert fa.value == "yearly (assumed)" and fa.contribution == -5
    assert any("assumed yearly" in c for c in assumed.caveats) and assumed.action != "BUY"
    # an explicit choice beats the verified claim
    chosen = bond_sig.assess(ltf, curve, p, {"freq": "2"}, day, (12, {"run_id": 12}))
    assert factor(chosen, "Coupon frequency").value == "half-yearly (chosen)"


def test_bond_provider_asks_for_the_verified_frequency_only_without_ctx_freq(monkeypatch):
    listed = parse_live_bonds(json.loads((FIX / "nse" / "bonds_live_trimmed.json").read_text()))
    asked = []

    async def bonds():
        return listed

    async def curve():
        return FALLBACK_CURVE

    async def verified(isin):
        asked.append(isin)
        return (12, {"kind": "verified", "claim_id": 1, "run_id": 12})

    async def profile():
        return Profile(tax_slab_pct=30)

    monkeypatch.setattr(bond_sig, "SOURCES", bond_sig.BondSources(bonds, curve, verified, profile,
                                                                  lambda: date(2026, 9, 28)))  # fmt: skip
    import asyncio

    s = asyncio.run(bond_sig.bond_signal("INE027E07998", {}))
    assert asked == ["INE027E07998"] and factor(s, "Coupon frequency").value == "monthly (verified)"
    asyncio.run(bond_sig.bond_signal("INE027E07998", {"freq": "1"}))
    assert asked == ["INE027E07998"]
