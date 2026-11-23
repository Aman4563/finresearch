"""#504 part A, the agent's side: `fincalc_functions()` lists every new function, `fincalc_call` runs each from JSON,
and `save_claim` re-executes a fincalc citation of each and reproduces the value (#242), grading it D when its
arguments are cited claims (#265). The stated values are the hand-worked ones of test_valuation_toolkit.py and
test_market_beta.py."""

from __future__ import annotations

import json

import pytest

URL = "https://example.com/source"
NEW = {
    "valuation.cost_of_equity_capm": "CAPM cost of equity",
    "valuation.after_tax_cost_of_debt": "After-tax cost of debt",
    "valuation.wacc": "WACC",
    "valuation.dcf_explicit": "DCF of year-by-year",
    "valuation.fcf_path": "Year-by-year revenue, EBIT, NOPAT and free cash flow",
    "valuation.dcf_checks": "Sanity flags for a DCF",
    "valuation.residual_income": "Residual-income value for a lender",
    "valuation.ddm": "Dividend-discount value",
    "market.beta": "OLS beta of a stock on an index",
}


def test_fincalc_functions_lists_the_new_functions_with_their_one_line_docs():
    from finresearch.mcp_server.server import fincalc_functions

    cat = json.loads(fincalc_functions())
    for name, starts in NEW.items():
        assert name in cat, name
        signature, doc = cat[name].split(" — ", 1)
        assert doc.startswith(starts) and "\n" not in doc and signature.startswith(name.split(".")[1] + "(")
    # helpers stay private: nothing underscore-prefixed, and no constant or dataclass, is callable by an agent
    assert not [k for k in cat if k.split(".")[1].startswith("_")]
    assert not {"valuation.DcfExplicit", "valuation.Wacc", "market.Beta"} & set(cat)
    # the existing entries are still there (the catalogue only grew)
    assert {"valuation.dcf", "valuation.justified_pb", "valuation.peer_stats", "market.price_return"} <= set(
        cat
    )


def test_fincalc_call_runs_each_from_json_and_returns_inputs_with_the_answer():
    """What an agent sends: strings and JSON lists. Each answer carries its inputs and constants."""
    from finresearch.mcp_server.server import fincalc_call

    def call(fn, **args):
        out = json.loads(fincalc_call(fn, args))
        assert "error" not in out, out
        return out["result"]

    ke = call("valuation.cost_of_equity_capm", risk_free="0.065", beta="1.2", equity_risk_premium="0.06")
    assert ke == {
        "risk_free": "0.065",
        "beta": "1.2",
        "equity_risk_premium": "0.06",
        "cost_of_equity": "0.137",
    }
    w = call("valuation.wacc", cost_of_equity="0.137", cost_of_debt_after_tax="0.0675", market_cap="8000",
             net_debt="-500")  # fmt: skip
    assert float(w["wacc"]) == 0.137 and w["net_cash"] is True and "understates the rate" in w["rule"]
    path = call("valuation.fcf_path", revenue="1000", growth_path=["0.10", "0.20"], ebit_margin_path=["0.20", "0.25"],
                tax_rate="0.25", da_pct="0.04", capex_pct="0.06", nwc_pct_of_change="0.10")  # fmt: skip
    assert [float(x) for x in path["fcf"]] == [133.0, 199.1] and "no refund" in path["tax_rule"]
    dcf = call("valuation.dcf_explicit", cash_flows=["-50", "100"], discount_rate="0.25", terminal_growth="0.05",
               terminal_metric="150", mid_year=False)  # fmt: skip
    assert float(dcf["value"]) == 360 and float(dcf["implied_exit_multiple"]) == 3.5
    assert dcf["implied_terminal_growth"] is None and "implied_terminal_growth" in dcf["unknown"]
    chk = call("valuation.dcf_checks", discount_rate="0.12", terminal_growth="0.12")
    assert chk["status"] == "error" and chk["flags"][0]["name"] == "terminal_growth_at_or_above_discount_rate"
    assert (
        chk["bands"]["terminal_share_high"] == "0.75" and "terminal_share_outside_band" in chk["not_checked"]
    )
    ri = call("valuation.residual_income", book_value="100", roe_path=["0.45"], cost_of_equity="0.25",
              payout_ratio="0.5", fade_years=2)  # fmt: skip
    assert float(ri["value"]) == 123.84 and ri["fade_years"] == 2
    assert float(call("valuation.ddm", dividend="10", growth="0.05", cost_of_equity="0.25")["value"]) == 52.5
    stats = call("valuation.peer_stats", peers=["54.85", "46.69", "42.69", "26.29"])
    assert (stats["min"], stats["max"]) == ("26.29", "54.85")
    # a refusal reaches the agent as an error with the reason, never as a number
    bad = json.loads(fincalc_call("valuation.dcf_explicit", {"cash_flows": ["100"], "discount_rate": "0.10",
                                                             "terminal_growth": "0.10"}))  # fmt: skip
    assert bad["error"] == "ValueError: discount_rate must exceed terminal_growth" and "result" not in bad


# --------------------------------------------------------------------------- save_claim re-executes each (#242)
@pytest.fixture
def ledger(env, tmp_path):
    """(save, grade, claim): `claim(value, unit)` stores an input claim and returns its id; `save(fn, args, key,
    inputs, value, unit, **citation extras)` stores a claim citing the fincalc call and returns save_claim's answer."""
    from finresearch.db import session_scope
    from finresearch.db.models import Claim, ResearchRun
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.mcp_server.claims import save_claim
    from finresearch.mcp_server.server import run_fincalc
    from finresearch.verify.evidence import claim_grade

    with session_scope() as s:
        co = get_or_create_company(s, "vt-" + tmp_path.name[-8:], "Synthetic Lender Ltd")
        run = ResearchRun(company_id=co.id, kind="stock_report", manifest={})
        s.add(run)
        s.flush()
        run_id = run.id
    n = iter(range(10_000))

    def _save(citations, value, unit, metric):
        with session_scope() as s:
            return save_claim(s, run_id=run_id, stream="stock_valuation", statement=f"{metric} {value} {unit}",
                              claim_type="numeric", citations=citations, metric=metric, value=value, unit=unit,
                              period="FY2026", fincalc=run_fincalc)  # fmt: skip

    def claim(value, unit="%"):
        return _save([{"url": URL, "quote": f"q{next(n)}"}], str(value), unit, f"input_{next(n)}")["claim_id"]

    def save(fn, args, key, inputs, value, unit, **extra):
        return _save([{"fincalc": {"function": fn, "args": args, "result_key": key}, "inputs": inputs, **extra}],
                     value, unit, fn.split(".")[1])  # fmt: skip

    def grade(res):
        with session_scope() as s:
            c = s.get(Claim, res["claim_id"])
            return claim_grade(c)["grade"], c.status, c.citations[0].computation

    return save, grade, claim


def _is_d(grade, res) -> dict:
    g, status, comp = grade(res)
    assert (g, status) == ("D", "unverified"), comp["detail"]
    assert comp["matches"] is True and comp["args_bound"] is True
    return comp


def test_save_claim_reruns_the_discount_rate_chain(ledger):
    """Risk-free 6.5 %, beta 1.2, premium 6 % -> 13.7 %; debt at 9 % less 25 % tax -> 6.75 %; on a market cap of
    ₹8,000 crore and net debt of ₹2,000 crore -> 12.31 %; with net cash of ₹500 crore -> the 13.7 % itself. Each
    figure is a claim computed from the claims before it."""
    save, grade, claim = ledger
    rf, beta, erp = claim("6.5"), claim("1.2", "x"), claim("6")
    ke = save("valuation.cost_of_equity_capm", {"risk_free": "0.065", "beta": "1.2", "equity_risk_premium": "0.06"},
              "cost_of_equity", [rf, beta, erp], "13.7", "%")  # fmt: skip
    assert _is_d(grade, ke)["bindings"] == {"beta": beta, "equity_risk_premium": erp, "risk_free": rf}
    kd = save("valuation.after_tax_cost_of_debt", {"pre_tax": "0.09", "tax_rate": "0.25"}, "after_tax",
              [claim("9"), claim("25")], "6.75", "%")  # fmt: skip
    _is_d(grade, kd)
    cap, debt, cash = claim("8000", "INR crore"), claim("2000", "INR crore"), claim("-500", "INR crore")
    args = {
        "cost_of_equity": "0.137",
        "cost_of_debt_after_tax": "0.0675",
        "market_cap": "8000",
        "net_debt": "2000",
    }
    w = save("valuation.wacc", args, "wacc", [ke["claim_id"], kd["claim_id"], cap, debt], "12.31", "%")
    _is_d(grade, w)
    net_cash = save("valuation.wacc", {**args, "net_debt": "-500"}, "wacc", [ke["claim_id"], kd["claim_id"], cap, cash],
                    "13.7", "%")  # fmt: skip
    _is_d(grade, net_cash)
    # the wrong figure is caught by the re-run: 12.5 % is not what the cited inputs give
    wrong = save("valuation.wacc", args, "wacc", [ke["claim_id"], kd["claim_id"], cap, debt], "12.5", "%")
    assert grade(wrong)[:2] == ("U", "unsupported") and "not the stated 12.5" in grade(wrong)[2]["detail"]
    # a result with several values needs the cited one named
    with pytest.raises(ValueError, match="name the one cited as result_key"):
        save("valuation.wacc", args, None, [ke["claim_id"], kd["claim_id"], cap, debt], "12.31", "%")


def test_save_claim_reruns_the_cash_flow_path_the_dcf_and_its_checks(ledger):
    """Year 1 of the hand-worked path: revenue 1,000 growing 10 % at a 20 % margin, tax 25 % -> FCF 133. The DCF of
    -50 and 100 at 25 % with 5 % terminal growth -> 360, terminal share 93.33 %. Checks at 12 % / 5 % / 6.5 % /
    0.60 -> 0 errors and a spread of 7 points over terminal growth. The year-by-year path and the cash-flow list
    are not claims: they are declared as constants with their reason, as #265 asks."""
    save, grade, claim = ledger
    why = "ASSUMPTION: management guidance, see the assumption claims"
    path = save("valuation.fcf_path",
                {"revenue": "1000", "growth_path": ["0.10"], "ebit_margin_path": ["0.20"], "tax_rate": "0.25",
                 "da_pct": "0.04", "capex_pct": "0.06", "nwc_pct_of_change": "0.10"},
                "final_fcf", [claim("1000", "INR crore"), claim("25")], "133", "INR crore",
                constants={k: why for k in ("growth_path", "ebit_margin_path", "da_pct", "capex_pct",
                                            "nwc_pct_of_change")})  # fmt: skip
    assert _is_d(grade, path)["bindings"]["growth_path[0]"] == "constant"
    r, g = claim("25"), claim("5")
    dcf_args = {
        "cash_flows": ["-50", "100"],
        "discount_rate": "0.25",
        "terminal_growth": "0.05",
        "mid_year": False,
    }
    flows = {"cash_flows": "the FCF path computed by valuation.fcf_path from the assumption claims"}
    value = save("valuation.dcf_explicit", dcf_args, "value", [r, g], "360", "INR crore", constants=flows)
    _is_d(grade, value)
    share = save("valuation.dcf_explicit", dcf_args, "terminal_share", [r, g], "93.33", "%", constants=flows)
    _is_d(grade, share)
    # without the declaration the cash flows are figures nobody cited: reproduced, but not grade D
    bare = save("valuation.dcf_explicit", dcf_args, "value", [r, g], "360", "INR crore")
    assert (
        grade(bare)[0] == "U"
        and "cash_flows[0], cash_flows[1] match no cited input" in grade(bare)[2]["detail"]
    )
    chk_args = {
        "discount_rate": "0.12",
        "terminal_growth": "0.05",
        "risk_free": "0.065",
        "terminal_share": "0.60",
    }
    ins = [claim("12"), claim("5"), claim("6.5"), claim("60")]
    _is_d(grade, save("valuation.dcf_checks", chk_args, "spread_over_terminal_growth", ins, "7", "%"))
    _is_d(grade, save("valuation.dcf_checks", chk_args, "errors", ins, "0", "count"))
    _is_d(grade, save("valuation.dcf_checks", chk_args, "warnings", ins, "0", "count"))


def test_save_claim_reruns_the_lender_methods_and_the_peer_range(ledger):
    """Book ₹100 a share, ROE 18 %, Ke 13 %, payout 50 %, growth 9 % -> ₹225 (the steady-state closed form);
    dividend ₹10 growing 5 % at 25 % -> ₹52.5; the lowest of four peer multiples is 26.29."""
    save, grade, claim = ledger
    ri = save("valuation.residual_income",
              {"book_value": "100", "roe_path": ["0.18"], "cost_of_equity": "0.13", "payout_ratio": "0.5",
               "terminal_growth": "0.09"},
              "value", [claim("100", "INR per share"), claim("18"), claim("13"), claim("50"), claim("9")],
              "225", "INR per share")  # fmt: skip
    _is_d(grade, ri)
    d = save("valuation.ddm", {"dividend": "10", "growth": "0.05", "cost_of_equity": "0.25"}, "value",
             [claim("10", "INR per share"), claim("5"), claim("25")], "52.5", "INR per share")  # fmt: skip
    _is_d(grade, d)
    peers = ["54.85", "46.69", "42.69", "26.29"]
    lo = save("valuation.peer_stats", {"peers": peers}, "min", [claim(p, "x") for p in peers], "26.29", "x")
    _is_d(grade, lo)
    assert (
        grade(
            save(
                "valuation.peer_stats", {"peers": peers}, "max", [claim(p, "x") for p in peers], "54.85", "x"
            )
        )[0]
        == "D"
    )


def test_save_claim_reruns_beta_but_a_pasted_series_is_not_cited_evidence(ledger):
    """The exact-linear pair of test_market_beta (beta 2.5). save_claim runs market.beta again and reproduces 2.5,
    but 122 closes typed in by an agent are figures no claim holds: declared as constants they are "constants only"
    (#265), so the claim is not grade D. This is the behaviour today; a re-runnable citation for stock_beta's
    stored-data answer is the open decision recorded in the PR."""
    save, grade, _ = ledger
    args = {"stock_closes": [100, 400] * 30 + [100], "index_closes": [100, 200] * 30 + [100]}
    res = save("market.beta", args, "beta", [], "2.5", "x",
               constants={"stock_closes": "NSE closes read with nse_price_history",
                          "index_closes": "index closes read with index_history"})  # fmt: skip
    g, status, comp = grade(res)
    assert (
        comp["matches"] is True
        and comp["result"] == "2.5"
        and "fincalc reproduces the value" in comp["detail"]
    )
    assert (
        comp["args_bound"] is False
        and "constants only" in comp["detail"]
        and (g, status) == ("U", "unsupported")
    )
