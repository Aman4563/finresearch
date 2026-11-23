"""#505: which fund, bond and credit functions `fincalc_call` runs, and that a claim citing one is re-run (grade D).

Expected values are worked by hand in the comments, never read back from fincalc.
"""

from __future__ import annotations

import inspect
import json
from decimal import Decimal

import pytest

from finresearch.agents.roles import BOND_STREAMS, FUND_STREAMS, MCP, ROLES

# every public function of the three modules is either callable or left out for a stated reason: a new function
# fails this test until someone decides
EXPOSED = {
    "funds": {"annualised_return", "expense_drag"},
    "bonds": {"accrued_interest", "after_tax_par_yield", "after_tax_ytm", "clean_price", "current_yield",
              "dirty_price", "duration", "effective_annual", "ytm"},
    "credit": set(),
}  # fmt: skip
SERIES = "takes a NAV or return series: an agent holds a sample at most, no claim binds hundreds of points"
TEXT_ORDER = "plain-float helper: numbers passed as strings would be compared as text"
SCHEDULE = "a stepped-back schedule is a pricing convention, not the bond's stated terms; a list cannot be re-checked"
GRADE = "decided by a rating grade (a word): save_claim binds numeric arguments only, and the result has no source"
LEFT_OUT = {
    "funds": {
        "nav_on_or_before": SERIES, "trailing_return": SERIES, "rolling_return_series": SERIES,
        "rolling_returns": SERIES, "xirr": SERIES, "sip_outcome": SERIES + " (fund_sip_outcome is the tool)",
        "sharpe_ratio": SERIES, "sortino_ratio": SERIES, "sharpe_ratio_excess": SERIES,
        "sortino_ratio_excess": SERIES, "downside_capture": SERIES, "r_squared": SERIES,
        "percentile_rank": TEXT_ORDER, "median": TEXT_ORDER, "wilson_interval": TEXT_ORDER,
        "effective_windows": TEXT_ORDER,
    },
    "bonds": {
        "coupon_dates": SCHEDULE, "previous_coupon": SCHEDULE, "cash_flows": SCHEDULE,
        "post_tax_yield": "right only for a bond bought at par (live bond run 12): after_tax_ytm and "
                          "after_tax_par_yield cover both cases",
    },
    "credit": {"base_grade": GRADE, "cumulative_default_rate": GRADE, "annual_pd": GRADE, "expected_loss": GRADE,
               "next_lower": GRADE},
}  # fmt: skip


def _call(function: str, **args):
    from finresearch.mcp_server import server

    return json.loads(server.fincalc_call(function, args))


@pytest.mark.parametrize("module", sorted(EXPOSED))
def test_every_fund_bond_and_credit_function_is_exposed_or_left_out_on_purpose(module):
    import importlib

    from finresearch.mcp_server import server

    mod = importlib.import_module(f"finresearch.fincalc.{module}")
    public = {n for n, f in inspect.getmembers(mod, inspect.isfunction)
              if not n.startswith("_") and f.__module__ == mod.__name__}  # fmt: skip
    assert public == EXPOSED[module] | set(LEFT_OUT[module]) and not EXPOSED[module] & set(LEFT_OUT[module])
    catalogue = json.loads(server.fincalc_functions())
    callable_now = {k.split(".", 1)[1] for k in catalogue if k.startswith(f"{module}.")}
    assert callable_now == EXPOSED[module]
    for name in LEFT_OUT[module]:
        assert server.fincalc_call(f"{module}.{name}", {}).startswith("unknown function"), name
        with pytest.raises(ValueError, match="unknown function"):  # save_claim's re-run refuses it too
            server.run_fincalc(f"{module}.{name}", {})


def test_the_roles_told_to_compute_fund_and_bond_figures_may_call_fincalc():
    users = {*FUND_STREAMS, *BOND_STREAMS, "fund_planner", "bond_planner", "fund_synthesizer", "bond_synthesizer",
             "verifier", "verifier_blind"}  # fmt: skip
    assert not [n for n in sorted(users) if f"{MCP}fincalc_call" not in ROLES[n].tools]


def test_expense_drag_runs_as_the_costs_prompt_asks():
    """₹1 lakh for 10 years at 12 % gross, TER 2 % against 1 %: 100000 x (1.11^10 - 1.10^10) = 100000 x
    (2.8394210 - 2.5937425) = 24,567.85."""
    out = _call("funds.expense_drag", amount="100000", years=10, gross_return="0.12", ter_high="0.02",
                ter_low="0.01")  # fmt: skip
    assert Decimal(out["result"]).quantize(Decimal("0.01")) == Decimal("24567.85")
    assert "expense_drag" in ROLES["fund_costs"].template_text()


def test_annualised_return_takes_iso_dates():
    """100 -> 121 over 1-Jan-2025 to 1-Jan-2027 (730 days): 1.21 ^ (365 / 730) - 1 = 10 %."""
    span = {"start": "2025-01-01", "end": "2027-01-01"}
    out = _call("funds.annualised_return", start_nav="100", end_nav="121", **span)
    assert Decimal(out["result"]).quantize(Decimal("0.000001")) == Decimal("0.100000")


PAR = {"settlement": "2026-03-13", "maturity": "2029-03-13", "coupon_rate": "0.08", "freq": 1, "face": "100"}


def test_bond_functions_on_a_three_year_annual_par_bond():
    """8 % annual, 3 years to run, settled on a coupon date. At par it yields its coupon and nothing has accrued.
    Cash flows 8, 8, 108 discounted at 8 %: 7.407407 + 6.858711 + 85.733882 = 100.
    Macaulay = (1 x 7.407407 + 2 x 6.858711 + 3 x 85.733882) / 100 = 2.783265 years; modified = / 1.08 = 2.577097;
    convexity = (2 x 7.407407 + 6 x 6.858711 + 12 x 85.733882) / (100 x 1.08^2) = 1084.773662 / 116.64 = 9.300186.
    At 9 %: 8/1.09 + 8/1.1881 + 108/1.295029 = 7.339450 + 6.733440 + 83.395816 = 97.468705."""
    assert Decimal(_call("bonds.ytm", clean="100", **PAR)["result"]) == Decimal("0.08")
    assert Decimal(_call("bonds.accrued_interest", **PAR)["result"]) == 0
    assert Decimal(_call("bonds.dirty_price", yld="0.08", **PAR)["result"]).quantize(Decimal("0.0001")) == 100
    d = _call("bonds.duration", yld="0.08", **PAR)["result"]
    assert d == {"macaulay": "2.783265", "modified": "2.577097", "convexity": "9.300186"}
    up = Decimal(_call("bonds.clean_price", yld="0.09", **PAR)["result"])
    assert up.quantize(Decimal("0.0001")) == Decimal("97.4687")
    # a par bond has no capital gain: after 30 % tax on the coupons it yields 8 % x 0.7 = 5.6 %
    after_tax = _call("bonds.after_tax_ytm", clean="100", tax_rate="0.30", **PAR)["result"]
    assert Decimal(after_tax) == Decimal("0.056")


def test_bond_yield_helpers_and_accrual_between_coupon_dates():
    # 90 days after the coupon date in a year without 29 February: 90 x 8 / 365 = 1.972603
    ai = _call("bonds.accrued_interest", **{**PAR, "settlement": "2026-06-11"})["result"]
    assert Decimal(ai).quantize(Decimal("0.000001")) == Decimal("1.972603")
    # coupon 89.80 on a clean price of 1072.0636: 8.3764 %
    cy = _call("bonds.current_yield", clean="1072.0636", coupon_rate="0.0898", face="1000")["result"]
    assert Decimal(cy).quantize(Decimal("0.000001")) == Decimal("0.083764")
    # 7 % compounded half-yearly: 1.035^2 - 1 = 7.1225 %
    assert Decimal(_call("bonds.effective_annual", nominal="0.07", freq=2)["result"]) == Decimal("0.071225")
    # a 6.40 % quarterly FD at a 30 % slab: (1 + 0.064 x 0.7 / 4)^4 - 1 = 1.0112^4 - 1 = 4.5558 %
    fd = _call("bonds.after_tax_par_yield", nominal="0.064", tax_rate="0.30", freq=4)["result"]
    assert Decimal(fd).quantize(Decimal("0.000001")) == Decimal("0.045558")
    # a refused input is an error, never a number
    assert "error" in _call("bonds.ytm", clean="100", **{**PAR, "freq": 3})


# --------------------------------------------------------------------------- a claim citing one is re-run (#242)
def _run(tmp_path) -> int:
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        co = get_or_create_company(s, "fc-" + tmp_path.name[-8:], "Synthetic Scheme")
        r = ResearchRun(company_id=co.id, kind="fund_report", manifest={})
        s.add(r)
        s.flush()
        return r.id


def _save(run_id: int, citations: list[dict], **kw) -> dict:
    from finresearch.db import session_scope
    from finresearch.mcp_server.claims import save_claim
    from finresearch.mcp_server.server import run_fincalc

    args = {"stream": "fund_costs", "claim_type": "numeric", "period": "2026-09-28", **kw}
    with session_scope() as s:
        return save_claim(s, run_id=run_id, citations=citations, fincalc=run_fincalc, **args)


def _grade(claim_id: int) -> tuple[str, str]:
    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.verify.evidence import claim_grade

    with session_scope() as s:
        c = s.get(Claim, claim_id)
        return claim_grade(c)["grade"], c.status


def _fact(run_id: int, metric: str, value: str, unit: str) -> int:
    web = [{"url": "https://www.example-amc.test/factsheet", "quote": f"{metric} {value}"}]
    return _save(run_id, web, statement=f"{metric} is {value} {unit}", metric=metric, value=value, unit=unit)[
        "claim_id"
    ]


def test_a_claim_citing_expense_drag_is_rerun_and_grade_d(env, tmp_path):
    """5-year return 12.76 %, regular TER 1.52 %, direct TER 0.84 %, ₹1 lakh for 10 years:
    100000 x (1.1192^10 - 1.1124^10) = 100000 x (3.0837348 - 2.9014147) = 18,232.01."""
    run_id = _run(tmp_path)
    ids = [_fact(run_id, "return_5y_annualised", "12.76", "%"), _fact(run_id, "ter_regular", "1.52", "%"),
           _fact(run_id, "ter_direct", "0.84", "%")]  # fmt: skip
    cite = {"fincalc": {"function": "funds.expense_drag",
                        "args": {"amount": "100000", "years": 10, "gross_return": "0.1276", "ter_high": "0.0152",
                                 "ter_low": "0.0084"}},
            "inputs": ids, "constants": {"amount": "illustration on ₹1 lakh", "years": "a 10-year holding period"}}  # fmt: skip
    kw = {"statement": "The regular plan costs ₹18,232 more over 10 years on ₹1 lakh", "unit": "INR",
          "metric": "expense_drag_10y"}  # fmt: skip
    good = _save(run_id, [cite], value="18232", **kw)
    assert good["citation_checks"][0]["quote_found"] is True
    assert _grade(good["claim_id"]) == ("D", "unverified")
    wrong = _save(run_id, [cite], value="24568", **kw)  # the figure for other inputs
    assert _grade(wrong["claim_id"]) == ("U", "unsupported")
    # TERs nobody cited: the arithmetic is right, the evidence is not there
    loose = {**cite, "inputs": ids[:1]}
    assert _grade(_save(run_id, [loose], value="18232", **kw)["claim_id"]) == ("U", "unsupported")


def test_a_bond_yield_claim_binds_price_coupon_frequency_and_face(env, tmp_path):
    """The par bond of the tests above bought at 97.4687, its price at a 9 % yield: so the YTM is 9 %. At 9 % the
    present values are 7.339450, 6.733440 and 83.395816 (sum 97.468705): Macaulay = (7.339450 + 2 x 6.733440 +
    3 x 83.395816) / 97.468705 = 2.780316 years, modified = 2.780316 / 1.09 = 2.550748."""
    run_id = _run(tmp_path)
    ids = [_fact(run_id, "clean_price", "97.4687", "INR per 100 face value"), _fact(run_id, "coupon_rate", "8", "%"),
           _fact(run_id, "coupon_frequency", "1", "payments/year"), _fact(run_id, "face_value", "100", "INR")]  # fmt: skip
    ytm = {"fincalc": {"function": "bonds.ytm", "args": {"clean": "97.4687", **PAR}}, "inputs": ids}
    y = _save(run_id, [ytm], stream="bond_pricing", statement="Yield to maturity is 9 %", metric="ytm",
              value="9", unit="%")  # fmt: skip
    assert _grade(y["claim_id"]) == ("D", "unverified"), y
    # the duration at that yield: the yield argument is the computed claim, the rest are the bond's terms
    cite = {"fincalc": {"function": "bonds.duration", "args": {"yld": "0.09", **PAR}, "result_key": "modified"},
            "inputs": [*ids[1:], y["claim_id"]]}  # fmt: skip
    kw = {"stream": "bond_pricing", "statement": "Modified duration is 2.5507 years", "unit": "years",
          "metric": "modified_duration"}  # fmt: skip
    assert _grade(_save(run_id, [cite], value="2.5507", **kw)["claim_id"]) == ("D", "unverified")
    macaulay = _save(run_id, [cite], value="2.7803", **kw)  # the other duration: not what result_key names
    assert _grade(macaulay["claim_id"]) == ("U", "unsupported")
    # a price the cited claims do not hold: right arithmetic, no evidence
    loose = {"fincalc": {"function": "bonds.ytm", "args": {"clean": "100", **PAR}}, "inputs": ids}
    res = _save(run_id, [loose], stream="bond_pricing", statement="Yield to maturity is 8 %", metric="ytm",
                value="8", unit="%")  # fmt: skip
    assert _grade(res["claim_id"]) == ("U", "unsupported")
