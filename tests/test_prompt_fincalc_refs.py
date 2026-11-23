"""#505: a prompt or skill may only name a fincalc function the `fincalc_call` tool will run, and a tool the role has.

`stream_fund_performance.md` told the agent to use "fincalc funds.sip_outcome" and `stream_fund_costs.md` "fincalc
funds.expense_drag" while `funds` was not in the MCP server's catalogue, so both calls answered "unknown function".
The scan below reads every file under agents/prompts and agents/skills, so it cannot come back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from finresearch.agents.roles import MCP, ROLES

AGENTS = Path(__file__).parents[1] / "src" / "finresearch" / "agents"
FINCALC = Path(__file__).parents[1] / "src" / "finresearch" / "fincalc"
# every module file of the fincalc package, catalogued or not: "forensic.piotroski" in a prompt must be caught too
MODULES = sorted(p.stem for p in FINCALC.glob("*.py") if p.stem != "__init__")
_MOD = "|".join(MODULES)
# "fincalc funds.sip_outcome" / "fincalc `x.y`": whatever follows the word fincalc as <module>.<function>
AFTER_FINCALC = re.compile(r"\bfincalc[ :`]+([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)")
# `numbers.parse_number`, dates.bidding_day_number, fincalc.market.max_drawdown: a fincalc module name, a dot, a name
QUALIFIED = re.compile(rf"(?<![\w/-])(?:fincalc\.)?({_MOD})\.([a-z_][a-z0-9_]*)")
# "fincalc.market": the module on its own
MODULE_ONLY = re.compile(r"\bfincalc\.([a-z_][a-z0-9_]*)\b(?!\.[a-z_])")
# "fincalc shares_from_amount", "via fincalc: allotment_probability_floor": a snake_case name right after the word
# (one underscore at least, so "fincalc from [C12]" and "fincalc valuation functions" are not function names)
BARE = re.compile(r"\bfincalc[ :`]+([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b(?![.(\w])")


def refs(text: str) -> tuple[set[str], set[str], set[str]]:
    """(qualified "module.function" names, modules named on their own, bare function names) in a prompt text."""
    qualified = {f"{m}.{f}" for m, f in AFTER_FINCALC.findall(text)} | {
        f"{m}.{f}" for m, f in QUALIFIED.findall(text)
    }
    modules = {m for m in MODULE_ONLY.findall(text) if m in MODULES}
    return qualified, modules, set(BARE.findall(text))


def _files() -> list[Path]:
    return sorted([*(AGENTS / "prompts").glob("*.md"), *(AGENTS / "skills").rglob("*.md")])


def _catalogue() -> set[str]:
    from finresearch.mcp_server import server

    return set(json.loads(server.fincalc_functions()))


def test_the_scan_reads_the_three_ways_a_prompt_names_a_function():
    text = ("Use fincalc funds.expense_drag over 10 years; label the day (fincalc dates.bidding_day_number). Use "
            "fincalc `numbers.parse_number`. Plain ratios.roce and `growth.cagr` count; fincalc.market for prices; "
            "fresh shares (fincalc shares_from_amount); odds via fincalc: allotment_probability_floor is a floor. "
            "Not functions: fincalc from [C12], fincalc valuation functions, fincalc_call, www.nseindia.com, e.g. "
            "the market.")  # fmt: skip
    qualified, modules, bare = refs(text)
    assert qualified == {"funds.expense_drag", "dates.bidding_day_number", "numbers.parse_number", "ratios.roce",
                         "growth.cagr"}  # fmt: skip
    assert modules == {"market"} and bare == {"shares_from_amount", "allotment_probability_floor"}
    # a module that is no fincalc file is still read after the word fincalc, so it cannot pass unseen
    assert refs("via fincalc nosuch.thing")[0] == {"nosuch.thing"}


def test_the_scan_sees_the_references_the_prompts_and_skills_hold_today():
    """Not vacuous: the names known to be in the files are found where they are."""
    found = {p.name if p.parent.name == "prompts" else p.parent.name: refs(p.read_text()) for p in _files()}
    assert len(_files()) >= 50 and "dates.bidding_day_number" in found["stream_demand.md"][0]
    assert "numbers.parse_number" in found["indian-fin-glossary"][0]
    assert "market" in found["stream_stock_technical.md"][1]
    assert {"shares_from_amount"} <= found["stream_valuation.md"][2]
    assert "funds.expense_drag" in found["stream_fund_costs.md"][0]


def test_every_fincalc_function_a_prompt_or_skill_names_is_in_the_catalogue():
    cat = _catalogue()
    functions = {name.split(".", 1)[1] for name in cat}
    missing: dict[str, list[str]] = {}
    for p in _files():
        qualified, modules, bare = refs(p.read_text())
        bad = ({q for q in qualified if q not in cat}
               | {f"{m}.*" for m in modules if not any(n.startswith(f"{m}.") for n in cat)}
               | {b for b in bare if b not in functions})  # fmt: skip
        for name in sorted(bad):
            missing.setdefault(name, []).append(str(p.relative_to(AGENTS)))
    assert not missing, f"named in a prompt or skill but refused by fincalc_call: {missing}"


def test_a_role_told_to_use_fincalc_or_a_tool_has_it_in_its_allow_list():
    """Per role, its own template (the house rules and the skills are shared by roles without fincalc_call)."""
    import asyncio

    from finresearch.mcp_server import server

    tools = {t.name for t in asyncio.run(server.server.list_tools())}
    bad = []
    for name, role in sorted(ROLES.items()):
        text = role.template_text()
        if any(refs(text)) and f"{MCP}fincalc_call" not in role.tools:
            bad.append(f"{name}: {role.template} names fincalc functions but the role has no fincalc_call")
        named = {t for t in tools if re.search(rf"(?<![\w.]){re.escape(t)}(?!\w)", text)}
        bad += [f"{name}: {role.template} names the tool {t}, which the role may not call"
                for t in sorted(named) if f"{MCP}{t}" not in role.tools]  # fmt: skip
    assert not bad, bad
