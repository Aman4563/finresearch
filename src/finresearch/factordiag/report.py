"""RESULTS.md of the stock factor diagnostics (#506): a short report of study.run's dictionary. Unknown is printed as
"unknown", never as 0; the reading of each outcome is the registered text (specs.READING)."""

from __future__ import annotations

from typing import Any

from finresearch.factordiag import specs

UNKNOWN = "unknown"


def _n(x: float | None, digits: int = 3) -> str:
    return UNKNOWN if x is None else f"{x:+.{digits}f}"


def _p(x: float | None) -> str:
    return UNKNOWN if x is None else f"{x:.4f}"


def _u(x: float | None, digits: int = 3) -> str:
    return UNKNOWN if x is None else f"{x:.{digits}f}"


def _pct(x: float | None, digits: int = 2) -> str:
    return UNKNOWN if x is None else f"{x * 100:+.{digits}f} %"


def _ci(s: dict[str, Any] | None, fmt=_n) -> str:
    """mean [lo, hi] of a series_stats block."""
    if s is None or s["mean"] is None:
        return UNKNOWN
    i = s["interval"]
    return f"{fmt(s['mean'])} [{fmt(i['lo'])}, {fmt(i['hi'])}]"


def _yes(x: bool | None) -> str:
    return UNKNOWN if x is None else ("**yes**" if x else "no")


def _ic_table(res: dict[str, Any]) -> list[str]:
    out = ["| Component | Months ahead | Months | Mean IC [95 % interval] | ICIR | IC > 0 | Newey-West t (reported "
           "only) | Blocks | p | Smallest p | q | Detected | Min. detectable IC |", "|" + "---|" * 13]  # fmt: skip
    for name, comp in res["components"].items():
        for h, c in comp["horizons"].items():
            sf = c["sign_flip"]
            share = UNKNOWN if c["share_positive"] is None else f"{c['share_positive'] * 100:.0f} %"
            out.append(f"| `{name}` | {h} | {c['months']} | {_ci(c)} | {_n(c['icir'], 2)} | {share} | "
                       f"{_n(c['newey_west']['t'], 2)} | {sf['blocks']} | {_p(sf['p'])} | {_p(sf['min_p'])} | "
                       f"{_p(c['q'])} | {_yes(c['detected'])} | {_u(c['interval']['mde'])} |")  # fmt: skip
    return out


def _decay_table(res: dict[str, Any]) -> list[str]:
    segs = [f"{a}-{b}" for a, b in specs.SEGMENTS]
    out = ["| Component | " + " | ".join(f"Months {s.replace('-', ' to ')}" for s in segs) + " |",
           "|" + "---|" * (len(segs) + 1)]  # fmt: skip
    for name, comp in res["components"].items():
        out.append(f"| `{name}` | " + " | ".join(_ci(comp["decay"][s]) for s in segs) + " |")
    return out


def _period_table(res: dict[str, Any]) -> list[str]:
    out = ["| Component | Months ahead | Before: months, mean IC [interval] | From: months, mean IC [interval] | "
           "Difference of block means | p | q | Detected |", "|" + "---|" * 8]  # fmt: skip
    for name, comp in res["components"].items():
        for h, c in comp["horizons"].items():
            a, b, d = c["sub_periods"]["first"], c["sub_periods"]["second"], c.get("period_difference")
            tail = (f"{_n(d['difference'])} | {_p(d['p'])} | {_p(d['q'])} | {_yes(d['detected'])}" if d else
                    "descriptive | not tested | not tested | not tested")  # fmt: skip
            out.append(f"| `{name}` | {h} | {a['months']}, {_ci(a)} | {b['months']}, {_ci(b)} | {tail} |")
    return out


def _quintile_table(res: dict[str, Any]) -> list[str]:
    cost = res["costs"]["per_side"]
    out = ["| Component | Months ahead | Q1 (lowest score) | Q2 | Q3 | Q4 | Q5 | All names | Q5 − all [interval] | "
           "Q5 − Q1 [interval] | Rank order (rho, steps up of 4) | Turnover Q5 / Q1 | Break-even cost, long-only | "
           "Break-even, long-short |", "|" + "---|" * 14]  # fmt: skip
    for name, comp in res["components"].items():
        for h, c in comp["horizons"].items():
            q, t, be = c["quintiles"], c["turnover"], c["break_even"]
            means = [_pct(x) for x in q["mean_return"]] if q["mean_return"] else [UNKNOWN] * specs.QUANTILES
            mono = q["monotonicity"]
            order = UNKNOWN if not mono else f"{_n(mono['rho'], 2)}, {mono['steps_up']}"
            turn = f"{_pct(t['top'], 0).lstrip('+')} / {_pct(t['bottom'], 0).lstrip('+')}"
            out.append(f"| `{name}` | {h} | " + " | ".join(means) + f" | {_pct(q['universe_mean'])} | "
                       f"{_ci(q['top_minus_universe'], _pct)} | {_ci(q['top_minus_bottom'], _pct)} | {order} | "
                       f"{turn} | {_pct(be['long_only']['cost'], 3).lstrip('+')} | "
                       f"{_pct(be['long_short']['cost'], 3).lstrip('+')} |")  # fmt: skip
    out += ["", f"The base harness's own cost per side is {cost * 100:.3f} %. A break-even cost is unknown when the "
            "mean gross spread is not positive (no cost level makes it zero). Returns are per holding period of the "
            "stated months, not annualised."]  # fmt: skip
    return out


def _reading(res: dict[str, Any]) -> list[str]:
    out = [res["reading_always"], ""]
    for name, comp in res["components"].items():
        out.append(f"**`{name}`**")
        for note in comp["reading"]:
            h = note["horizon"]
            at = "" if h is None else f" ({h} month{'' if h == 1 else 's'} ahead)"
            why = f" {note['why']}." if note.get("why") else ""
            out.append(f"- *{note['label'].replace('_', ' ')}*{at}:{why} {note['text']}")
        out.append("")
    return out


def _unknowns(res: dict[str, Any]) -> list[str]:
    p = res["panel"]
    out = [
        f"- Not diagnosed, no point-in-time history: `{k}` ({v})."
        for k, v in res["unknown_components"].items()
    ]
    months: dict[str, int] = {}
    for comp in res["components"].values():
        for h, c in comp["horizons"].items():
            for reason, n in c["unknown_months"].items():
                key = f"{h} months ahead, {reason.replace('_', ' ')}"
                months[key] = max(months.get(key, 0), n)
    if months:
        out.append("- Months whose IC is unknown (the largest count over the components): "
                   + "; ".join(f"{k}: {n}" for k, n in months.items()) + ".")  # fmt: skip
    else:
        out.append("- No evaluated month had an unknown IC.")
    for w, f in p["flags"].items():
        if w.startswith("0-"):
            out.append(f"- Forward returns over {w[2:]} months: {f['jump']} name-dates unknown across an unexplained "
                       f"price jump, {f['gap']} unknown across a price gap, {f['ended']} held in cash from a last "
                       "close.")  # fmt: skip
    ended = ", ".join(f"{k} ({n})" for k, n in sorted(p["ended"].items())) or "none"
    missing = ", ".join(f"{k} ({n})" for k, n in sorted(p["members_without_prices"].items())) or "none"
    out += [f"- Stocks that stopped trading inside a window (decision dates affected): {ended}.",
            f"- Members with no price series (member-months): {missing}."]  # fmt: skip
    return out


LIMITS = (
    "Seen data, in sample, 30 tests in two families: a diagnosis, not a validation, and never an edge claim.",
    "A forward return across an unexplained price jump is unknown (the base harness's rule): this drops some real "
    "moves, is selection on the outcome and grows with the horizon. The counts are above.",
    "A stock that stopped trading is held in cash at 0 % from its last close; for a merger the holder really received "
    "the acquirer's shares.",
    "Price returns without dividends; decisions and prices at the same month-end close, as the base harness.",
    "The ICs are on raw returns, not returns in excess of the market or adjusted for beta. A score tied to beta (low "
    "volatility above all) can show an IC that is beta times the market's drift over the sample, not stock selection; "
    "the registered synthetic null has a market with no drift.",
    "The Newey-West t is reported only: on overlapping horizons it rejects a true null too often (8 to 15 % at 6 "
    "and 12 months on the registered synthetic null).",
    "At 12 months the sample holds about six two-year blocks, so the smallest p a cell can reach is printed beside "
    "it; 'not detected at 12 months' is weak evidence of absence.",
    "The plain top quintile here is not the registered B0 rule (B0 adds the trend gate and leaves empty slots in "
    "cash).",
)


def render(res: dict[str, Any]) -> str:
    p, a, fam = res["panel"], res["audit"] or {}, res["families"]
    el = p["eligible"] or {}
    audit = (f"passed on {a['dates']} decision dates ({a['score_checks']} score checks, {a['return_checks']} return "
             "checks)" if a.get("passed") else "not run")  # fmt: skip
    lines = [
        "# Factor diagnostics of the stock signal on the point-in-time Nifty 50: results", "",
        f"Generated {res['generated']} by `finresearch eval factor-diagnostics`. Registration `{res['spec_version']}`, "
        f"hash `{res['registration'][:12]}…`; `PREREG.md` (sha256 `{res['prereg_sha256'][:12]}…`) was committed and "
        "pushed before any price was read.", "",
        f"**{res['label'].capitalize()}.**", "",
        f"Panel: {p['decision_dates']} month-ends from {p['from']} to {p['to']}, {p['months_with_eligible_names']} "
        f"with eligible names (per month: {el.get('min', UNKNOWN)} to {el.get('max', UNKNOWN)}, median "
        f"{el.get('median', UNKNOWN)}). Look-ahead audit: {audit}.", "",
        f"## Rank IC by horizon (family A: {fam['A']['size']} tests, {fam['A']['known']} known, "
        f"{len(fam['A']['detected'])} detected at q ≤ {fam['A']['fdr']:.2f})", "", *_ic_table(res), "",
        "p is the registered block sign-flip randomisation; q is Benjamini-Hochberg over all 20 cells. The interval "
        "is Student t on block means.", "",
        "## Decay: mean IC against the return over each stretch ahead [95 % interval]", "", *_decay_table(res), "",
        f"## Sub-periods: decision dates before and from {specs.SPLIT:%d-%b-%Y} (family B: {fam['B']['size']} tests, "
        f"{fam['B']['known']} known, {len(fam['B']['detected'])} detected)", "", *_period_table(res), "",
        "The split is the half boundary already published in the base experiment's results: family B decomposes a "
        "seen difference and is not fresh evidence of decay.", "",
        "## Quintiles, turnover and break-even cost", "", *_quintile_table(res), "",
        "## Reading (the rules were registered before any price was read)", "", *_reading(res),
        "## What is unknown", "", *_unknowns(res), "",
        "## Limits", "", *(f"- {x}" for x in LIMITS), "",
    ]  # fmt: skip
    return "\n".join(lines)
