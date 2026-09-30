"""Listed bond / NCD signal: buy, hold or avoid (docs/dev/RESEARCH_ROADMAP.md §D.4).

The question is whether the bond pays enough, after tax and after the credit losses its rating grade has historically
suffered, to beat the risk-free alternatives an Indian retail investor actually has:

    y_net = post-tax YTM - PD_annual x LGD,   PD_annual = 1 - (1 - CDR_3y)**(1/3)

* post-tax YTM: `fincalc.bonds.after_tax_ytm`, coupons taxed at the slab (plus 4 % cess); the gap between face value
  and the clean price is a capital gain at redemption, taxed as LTCG at 12.5 % (+ cess) for a listed bond held more
  than 12 months, else at the slab (§D.7). A tax-free bond (ctx ``tax_free=1``) pays no tax on coupons.
* CDR: CRISIL's average cumulative default rates by grade, FY2016-26 [P29] (`fincalc.credit`). LGD: 60 % unsecured,
  40 % secured, an assumption ([U] in the roadmap) you can change with ``lgd`` or ``secured=1``.
* alternatives, post-tax: the FBIL G-sec par yield at the bond's remaining tenor (half-yearly coupons, slab-taxed)
  and a bank FD (SBI's card rate for the tenor by default, quarterly compounding; ``fd`` overrides it, in % a year).
* every yield is compared as an **effective annual** rate: a half-yearly G-sec at 6.66 % is 6.77 % a year.

Actions: BUY when y_net beats the matching G-sec by the required credit spread (defaults: the roadmap's 1.5 pp for AA
and 3 pp for A; ``min_spread`` in pp overrides) *and* beats the FD *and* the bond traded at least ₹1 lakh today;
AVOID when y_net does not even beat the better risk-free alternative (all the credit and liquidity risk, none of the
reward); HOLD otherwise; NO_SIGNAL when the price, coupon, maturity or rating is missing, the bond is partly redeemed,
or the grade is below BB (CRISIL's averages here don't cover it).

The probability is about a stated event: held to maturity (or to ``horizon_years``), the bond pays in full and its
post-tax yield beats the better alternative. The arithmetic is deterministic; the uncertainty is default (CRISIL's
base rate for the grade; the lower end of the range uses one grade worse) and, when sold before maturity, the level
of yields at the sale (±100 bp scenarios).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from finresearch.api.markets import TtlCache
from finresearch.fincalc import bonds as B
from finresearch.fincalc import credit as C
from finresearch.fincalc.dates import add_years, today_ist
from finresearch.signals.base import Factor, Signal, Validation, clip_score
from finresearch.signals.registry import register

NSE_BONDS_URL = "https://www.nseindia.com/market-data/bonds-traded-in-capital-market"
SBI_FD_URL = "https://sbi.bank.in/web/interest-rates/deposit-rates/retail-domestic-term-deposits"
CESS = Decimal("0.04")
LTCG_LISTED = Decimal("0.125")  # listed bonds held > 12 months, no indexation (from 23-Jul-2024, §D.7)
# SBI retail term-deposit card rates for the public (% a year), "Revised Rates w.e.f. 15/12/2025", page last updated
# 16-06-2026, read 30-Sep-2026: (up to years, rate).
FD_TABLE: list[tuple[float, Decimal]] = [
    (1.0, Decimal("5.90")), (2.0, Decimal("6.25")), (3.0, Decimal("6.40")), (5.0, Decimal("6.30")),
    (10.0, Decimal("6.05")),
]  # fmt: skip
FD_AS_OF = "SBI card rates w.e.f. 15-Dec-2025 (page updated 16-Jun-2026)"
# Extra post-tax yield over the matching G-sec needed before taking the credit risk (pp). AA 1.5 and A 3.0 are the
# roadmap's examples (§D.4); the others are this module's extrapolation [W]. Override with ctx `min_spread`.
REQUIRED_SPREAD = {"SOV": Decimal(0), "AAA": Decimal("0.5"), "AA": Decimal("1.5"), "A": Decimal("3.0"),
                   "BBB": Decimal("4.5"), "BB": Decimal("6.0")}  # fmt: skip
GRADE_POINTS = {"SOV": 5.0, "AAA": 5.0, "AA": 0.0, "A": -5.0, "BBB": -10.0, "BB": -20.0}
LIQUID_MIN_INR = Decimal(
    100000
)  # today's traded value below this: a quote you may not be able to deal at [W]
WELL_TRADED_INR = Decimal(1000000)
RATE_SHOCK_BP = 100

# factor names the bond page's yield ladder reads
LADDER = ("G-sec par yield", "G-sec after tax", "FD rate", "FD after tax", "Pre-tax YTM", "Post-tax YTM",
          "Post-tax, after expected loss")  # fmt: skip


@dataclass
class BondSources:
    bonds: Callable[[], Awaitable[list]] | None = None  # () -> [ListedBond]
    par_curve: Callable[[], Awaitable[Any]] | None = None  # () -> adapters.fbil.ParCurve
    verified_freq: Callable[[str], Awaitable[tuple[int, dict] | None]] | None = None
    profile: Callable[[], Awaitable[Any]] | None = None
    today: Callable[[], date] = today_ist


SOURCES = BondSources()
_CACHE = TtlCache()


async def _bonds() -> list:
    if SOURCES.bonds is not None:
        return await SOURCES.bonds()

    async def make():
        from finresearch.adapters.nse_bonds import live_bonds

        return await live_bonds()

    return await _CACHE.get("bonds", 300, make)


async def _par_curve():
    if SOURCES.par_curve is not None:
        return await SOURCES.par_curve()

    async def make():
        from finresearch.adapters.fbil import FALLBACK_CURVE, FbilClient

        try:
            async with FbilClient() as fbil:
                return await fbil.latest_par_curve()
        except Exception:
            return FALLBACK_CURVE

    return await _CACHE.get("fbil", 6 * 3600, make)


async def _verified_freq(isin: str):
    if SOURCES.verified_freq is not None:
        return await SOURCES.verified_freq(isin)
    from finresearch.api.markets import _verified_frequency

    try:
        return await asyncio.to_thread(_verified_frequency, isin)
    except Exception:
        return None


async def _profile():
    if SOURCES.profile is not None:
        return await SOURCES.profile()
    from finresearch.signals.fund import _profile as load

    return await load()


def fd_rate_for(years: float) -> Decimal:
    """SBI's card rate (fraction) for a deposit of about `years` (the longest bucket beyond ten years)."""
    for upto, rate in FD_TABLE:
        if years < upto:
            return rate / 100
    return FD_TABLE[-1][1] / 100


def _num(ctx: dict[str, Any], key: str, lo: float, hi: float) -> Decimal | None:
    v = ctx.get(key)
    if v in (None, ""):
        return None
    try:
        d = Decimal(str(v))
    except Exception as e:
        raise ValueError(f"{key} must be a number") from e
    if not lo <= d <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return d


def _pp(x: float | Decimal) -> float:
    return round(float(x) * 100, 3)


def holding_period_yield(dirty_buy: Decimal, settlement: date, exit_day: date, yld: float, maturity: date,
                         coupon: Decimal, freq: int, face: Decimal, coupon_tax: Decimal, gain_tax: Decimal,
                         clean_buy: Decimal) -> Decimal:  # fmt: skip
    """Effective annual post-tax return from buying at `dirty_buy` and selling on `exit_day` at yield `yld`:
    coupons in between taxed at `coupon_tax`, accrued interest in the sale price taxed as interest, and a capital gain
    on the clean price taxed at `gain_tax` (a loss saves nothing)."""
    from finresearch.fincalc.funds import xirr

    flows = [(settlement, -dirty_buy)]
    for cf in B.cash_flows(settlement, maturity, coupon, freq, face):
        if cf.day <= exit_day:
            flows.append((cf.day, cf.amount * (1 - coupon_tax)))
    ai = B.accrued_interest(exit_day, maturity, coupon, freq, face)
    clean_exit = B.dirty_price(yld, exit_day, maturity, coupon, freq, face) - ai
    gain = clean_exit - clean_buy
    flows.append((exit_day, clean_exit - (gain * gain_tax if gain > 0 else 0) + ai * (1 - coupon_tax)))
    return xirr(flows)


def assess(
    bond, curve, profile, ctx: dict[str, Any], today: date, verified: tuple[int, dict] | None
) -> Signal:
    """The bond signal from NSE's list row, the FBIL curve and the profile (pure: no I/O)."""
    factors: list[Factor] = []
    caveats: list[str] = []
    sources = [NSE_BONDS_URL, curve.source, C.CDR_SOURCE, SBI_FD_URL]
    event = "held to maturity, it pays in full and its post-tax yield beats the better of the matching G-sec and an FD"
    common = dict(asset="bond", instrument=bond.isin, name=f"{bond.symbol} {bond.series or ''}".strip(),
                  sources=sources, as_of=bond.as_of or datetime.now(UTC))  # fmt: skip
    rule_validation = Validation(
        status="rule_based",
        description="Deterministic yield arithmetic with golden tests; "
        "default risk is CRISIL's base rate for the grade.",
    )

    def no_signal(reason: str, horizon: str = "to maturity") -> Signal:
        caveats.insert(0, reason)
        return Signal(action="NO_SIGNAL", score=0.0, event=event, horizon=horizon, method="not computed",
                      validation=rule_validation, factors=factors, caveats=caveats, **common)  # fmt: skip

    if any("partly" in w for w in bond.warnings):
        return no_signal(bond.warnings[0])
    caveats.extend(
        f"NSE list: {w}." for w in bond.warnings
    )  # e.g. a past next-interest date: check the schedule
    price = bond.last_price or bond.close
    if not (price and bond.coupon_pct is not None and bond.maturity and bond.face_value):
        return no_signal("NSE's list lacks the price, coupon, maturity or face value.")
    if bond.maturity <= today:
        return no_signal(f"The bond matured on {bond.maturity}.")
    years = (bond.maturity - today).days / 365.25

    # --- inputs and assumptions
    freq_ctx = ctx.get("freq")
    if freq_ctx not in (None, ""):
        if str(freq_ctx) not in ("1", "2", "4", "12"):
            raise ValueError("freq must be 1, 2, 4 or 12")
        freq, freq_note, freq_kind = int(freq_ctx), "chosen", "chosen"
    elif verified:
        freq, freq_note = verified[0], f"verified in research run #{verified[1].get('run_id')}"
        freq_kind = "verified"
    else:
        freq, freq_note, freq_kind = 1, "assumed yearly", "assumed"
        caveats.append("Coupon frequency assumed yearly: NSE's list doesn't give it and no research run has verified "
                       "it. Check the offer document; the yield depends on it, so the range below spans every "
                       "frequency and the signal won't say BUY until it is known.")  # fmt: skip
    basis = ctx.get("basis") or "dirty"
    if basis not in ("dirty", "clean"):
        raise ValueError("basis must be 'dirty' or 'clean'")
    slab = _num(ctx, "tax_slab_pct", 0, 50)
    slab = slab if slab is not None else Decimal(str(getattr(profile, "tax_slab_pct", 30)))
    tax = slab / 100 * (1 + CESS)
    tax_free = str(ctx.get("tax_free", "")).lower() in ("1", "true", "yes")
    coupon_tax = Decimal(0) if tax_free else tax
    gain_tax = LTCG_LISTED * (1 + CESS) if years > 1 else tax
    rating_text = ctx.get("rating") or bond.rating
    grade = C.base_grade(rating_text)
    lgd = _num(ctx, "lgd", 0, 1)
    secured = str(ctx.get("secured", "")).lower() in ("1", "true", "yes")
    lgd = lgd if lgd is not None else (C.LGD_SECURED if secured else C.LGD_UNSECURED)
    horizon_years = _num(ctx, "horizon_years", 0.25, 50)
    hold = float(horizon_years) if horizon_years is not None and float(horizon_years) < years - 0.25 else None
    horizon = f"{hold:g} years (sold before maturity)" if hold else f"to maturity ({years:.1f} years)"

    # --- the bond's own yields
    coupon, face = bond.coupon_pct / 100, bond.face_value
    ai = B.accrued_interest(today, bond.maturity, coupon, freq, face)
    clean = price - ai if basis == "dirty" else price
    dirty = price if basis == "dirty" else price + ai
    if clean <= 0:
        return no_signal("The clean price would be negative: check the price basis.")
    ytm = B.ytm(clean, today, bond.maturity, coupon, freq, face)
    ytm_eff = B.effective_annual(ytm, freq)
    ata = B.after_tax_ytm(clean, today, bond.maturity, coupon, freq, coupon_tax, capital_gains_rate=gain_tax,
                          face=face, accrued=ai)  # fmt: skip
    ata_eff = B.effective_annual(ata, freq)

    # --- alternatives
    tenor = hold or years
    gsec_semi = curve.par_yield(tenor)
    gsec_eff = B.effective_annual(gsec_semi, 2)
    gsec_post = B.after_tax_par_yield(gsec_semi, tax, 2)
    fd_ctx = _num(ctx, "fd", 0, 20)
    fd = fd_ctx / 100 if fd_ctx is not None else fd_rate_for(tenor)
    fd_eff = B.effective_annual(fd, 4)
    fd_post = B.after_tax_par_yield(fd, tax, 4)
    alt = max(gsec_post, fd_post)
    alt_name = "G-sec" if gsec_post >= fd_post else "FD"
    curve_note = f"FBIL par yield curve of {curve.as_of:%d-%b-%Y}" + (" (FBIL unreachable: stored copy)"
                                                                      if curve.fallback else "")  # fmt: skip
    fd_note = (
        f"your FD rate {fd * 100:.2f} %"
        if fd_ctx is not None
        else f"{FD_AS_OF}: {fd * 100:.2f} % for this tenor"
    )

    ladder = [
        (LADDER[0], gsec_eff, f"{curve_note}, at {tenor:.2f} years; {gsec_semi * 100:.2f} % half-yearly is "
                              f"{gsec_eff * 100:.2f} % a year.", curve.source),
        (LADDER[1], gsec_post, f"G-sec coupons are taxed at your slab ({slab}% + 4 % cess).", curve.source),
        (LADDER[2], fd_eff, f"{fd_note}, compounded quarterly.", SBI_FD_URL if fd_ctx is None else None),
        (LADDER[3], fd_post, "FD interest is taxed at your slab every year.", SBI_FD_URL if fd_ctx is None else None),
        (LADDER[4], ytm_eff, f"Yield to maturity at the {basis} price {price} (coupon {freq_note}), as an annual "
                             f"rate.", NSE_BONDS_URL),
        (LADDER[5], ata_eff, ("Coupons tax-free (you said so); " if tax_free else f"Coupons taxed at {slab}% + cess; ")
         + f"the gap to face value taxed as {'LTCG 12.5 %' if years > 1 else 'income'} + cess at redemption (the "
           "bond page's after-tax yield taxes it at the slab, so the two can differ).", "fincalc:bonds.after_tax_ytm"),
    ]  # fmt: skip
    for name, v, text, src in ladder:
        factors.append(Factor(name, _pp(v), 0.0, text, source=src, unit="% a year"))
    freq_names = {1: "yearly", 2: "half-yearly", 4: "quarterly", 12: "monthly"}
    factors.append(Factor("Coupon frequency", f"{freq_names[freq]} ({freq_kind})", -5.0 if freq_kind == "assumed" else 0.0,
                          {"verified": f"Paid {freq_names[freq]}, {freq_note} (a verified coupon_frequency claim).",
                           "chosen": f"Paid {freq_names[freq]}, as you chose.",
                           "assumed": "Not known: yearly is assumed. The offer document gives it."}[freq_kind],
                          source=(f"claim:{verified[1].get('claim_id')}" if freq_kind == "verified" and verified
                                  else None)))  # fmt: skip

    if grade is None:
        return no_signal(f"No usable long-term rating ({rating_text or 'none in NSE list'}): the expected loss needs "
                         "one. Pass rating=… if you know it (tax-free PSU bonds are usually AAA).", horizon)  # fmt: skip
    if grade not in C.CDR and grade != "SOV":
        return no_signal(f"Rated {grade}: below BB, where CRISIL's averages used here give no default rate. Treat it "
                         "as speculative.", horizon)  # fmt: skip

    el = C.expected_loss(grade, lgd) if grade != "SOV" else Decimal(0)
    y_net = ata_eff - el
    factors.append(Factor("Expected credit loss", _pp(el), 0.0,
                          f"{grade}: CRISIL 3-year cumulative default rate {C.CDR.get(grade, (0, 0, 0))[2] * 100:.2f} % "
                          f"→ {C.annual_pd(grade) * 100:.3f} % a year × {lgd * 100:.0f} % loss given default "
                          f"({'your figure' if ctx.get('lgd') else 'secured' if secured else 'unsecured, assumed'}).",
                          source=C.CDR_SOURCE, unit="pp a year"))  # fmt: skip
    factors.append(Factor(LADDER[6], _pp(y_net), 0.0, "Post-tax yield minus the expected credit loss.",
                          source="fincalc:credit.expected_loss", unit="% a year"))  # fmt: skip

    # --- scored factors
    required = _num(ctx, "min_spread", 0, 20)
    required = required / 100 if required is not None else REQUIRED_SPREAD[grade] / 100
    spread = y_net - gsec_post
    factors.append(Factor("Spread over G-sec (after tax and loss)", _pp(spread),
                          round(max(-40.0, min(40.0, 20 * float((spread - required) * 100))), 1),
                          f"{_pp(spread):+.2f} pp over the matching G-sec after tax; you ask {_pp(required):.2f} pp "
                          f"for a {grade} credit.", unit="pp"))  # fmt: skip
    vs_fd = y_net - fd_post
    factors.append(Factor("Versus FD (after tax and loss)", _pp(vs_fd),
                          round(max(-20.0, min(20.0, 10 * float(vs_fd * 100))), 1),
                          f"{_pp(vs_fd):+.2f} pp against the FD after tax.", unit="pp"))  # fmt: skip
    factors.append(Factor("Rating grade", rating_text, GRADE_POINTS.get(grade, 0.0),
                          f"Lowest grade across agencies: {grade}. CRISIL's study covers CRISIL ratings; other "
                          f"agencies' grades are treated as equal.", source=C.CDR_SOURCE))  # fmt: skip
    tv = bond.traded_value
    liq = (
        5.0
        if tv and tv >= WELL_TRADED_INR
        else 0.0
        if tv and tv >= LIQUID_MIN_INR
        else -15.0
        if tv
        else -20.0
    )
    factors.append(Factor("Traded today on NSE", float(tv) if tv else None, liq,
                          "Today's traded value in NSE's capital-market segment. A thinly traded NCD's quote may not "
                          "be a price you can deal at, and selling early can cost more than the spread.",
                          source=NSE_BONDS_URL, unit="₹"))  # fmt: skip
    dur = B.duration(ytm, today, bond.maturity, coupon, freq, face)
    up = B.dirty_price(float(ytm) + RATE_SHOCK_BP / 10000, today, bond.maturity, coupon, freq, face)
    shock = float(up / dirty - 1) if basis == "dirty" else float(up / (clean + ai) - 1)
    factors.append(Factor("Rate sensitivity", float(dur.modified), -min(15.0, 3 * float(dur.modified)) if hold else 0.0,
                          f"Modified duration {dur.modified:.2f}: if yields rise 1 pp the price falls about "
                          f"{-shock * 100:.1f} %. " + ("You plan to sell before maturity, so this is a real risk."
                                                         if hold else "Held to maturity, price swings don't change the "
                                                                      "yield you lock in."),
                          source="fincalc:bonds.duration", unit="years"))  # fmt: skip
    score = clip_score(sum(f.contribution for f in factors))

    # --- when the frequency is only assumed, would another schedule flip the comparison?
    freq_flips = False
    if freq_kind == "assumed":
        outcomes = set()
        for f in (1, 2, 4, 12):
            ai_f = B.accrued_interest(today, bond.maturity, coupon, f, face)
            clean_f = price - ai_f if basis == "dirty" else price
            if clean_f <= 0:
                continue
            ata_f = B.effective_annual(B.after_tax_ytm(clean_f, today, bond.maturity, coupon, f, coupon_tax,
                                                       capital_gains_rate=gain_tax, face=face, accrued=ai_f), f)  # fmt: skip
            outcomes.add(ata_f > alt)
        freq_flips = len(outcomes) > 1

    # --- the event's probability
    if hold:
        exit_day = today + timedelta(days=round(hold * 365.25))
        irr = {bp: holding_period_yield(dirty, today, exit_day, float(ytm) + bp / 10000, bond.maturity, coupon, freq,
                                        face, coupon_tax, LTCG_LISTED * (1 + CESS) if hold > 1 else tax, clean)
               for bp in (-RATE_SHOCK_BP, 0, RATE_SHOCK_BP)}  # fmt: skip
        p_def = C.cumulative_default_rate(grade, hold) or Decimal(0)
        lower_grade = C.next_lower(grade)
        p_def_low = C.cumulative_default_rate(lower_grade, hold) if lower_grade else p_def
        p = float(1 - p_def) if irr[0] > alt else 0.0
        interval = (float(1 - (p_def_low or p_def)) if irr[RATE_SHOCK_BP] > alt else 0.0,
                    float(1 - p_def) if irr[-RATE_SHOCK_BP] > alt else 0.0)  # fmt: skip
        event = (f"sold after {hold:g} years, it pays in full and its post-tax return beats the better of the "
                 f"matching G-sec and an FD")  # fmt: skip
        caveats.append(f"Selling after {hold:g} years: at today's yield the post-tax return is "
                       f"{_pp(irr[0]):.2f} % a year, {_pp(irr[RATE_SHOCK_BP]):.2f} % if yields are 1 pp higher then, "
                       f"{_pp(irr[-RATE_SHOCK_BP]):.2f} % if 1 pp lower (vs {_pp(alt):.2f} % from the {alt_name}); "
                       f"the range spans these rate scenarios and a one-grade downgrade.")  # fmt: skip
    else:
        p_def = C.cumulative_default_rate(grade, years) or Decimal(0)
        lower_grade = C.next_lower(grade)
        p_def_low = C.cumulative_default_rate(lower_grade, years) if lower_grade else p_def
        beats = ata_eff > alt
        p = float(1 - p_def) if beats else 0.0
        interval = (float(1 - (p_def_low or p_def)) if beats else 0.0, p)
        if not beats:
            caveats.append(f"Even if it pays in full, its post-tax yield {_pp(ata_eff):.2f} % doesn't beat the "
                           f"{alt_name}'s {_pp(alt):.2f} %: the event fails without any default.")  # fmt: skip
    if freq_flips:
        interval = (0.0, max(interval[1], float(1 - p_def)))
        caveats.append("With another coupon frequency the bond would land on the other side of the alternative: "
                       "the outcome hangs on the schedule you haven't confirmed.")  # fmt: skip
    if years > 3:
        caveats.append("CRISIL publishes default rates up to 3 years; beyond that the 3-year rate is extended at a "
                       "constant annual rate.")  # fmt: skip
    caveats.append(f"Default rates are CRISIL's {C.CDR_AS_OF}: a base rate for the grade, not a forecast for this "
                   "issuer. A downgrade can cut the price long before any default.")  # fmt: skip
    caveats.append("LGD (loss given default) of 60 % unsecured / 40 % secured is an assumption; NSE's list doesn't "
                   "say whether this NCD is secured.")  # fmt: skip

    # --- action
    liquid = bool(tv and tv >= LIQUID_MIN_INR)
    if y_net <= alt:
        action = "AVOID"
    elif spread >= required and y_net > fd_post and liquid and grade in C.INVESTMENT_GRADE:
        action = "BUY"
    else:
        action = "HOLD"
    if action == "BUY" and freq_kind == "assumed":
        action = "HOLD"  # lower confidence: the yield itself rests on a guessed schedule
    if action == "AVOID" and freq_flips:
        action = "HOLD"  # another schedule would beat the alternative: not enough to say avoid
    if action == "HOLD" and not liquid:
        caveats.append("Little or no trading today: check the last few sessions' volumes before buying.")
    method = (f"§D.4 expected-loss-adjusted post-tax yield vs post-tax FBIL G-sec and FD (effective annual rates); "
              f"EL = PD_annual × LGD from CRISIL {C.CDR_AS_OF}")  # fmt: skip
    validation = Validation(status="base_rate", metrics={"no_default_rate": round(float(1 - p_def), 6), "years": round(hold or years, 2)},
                            description="Yields are deterministic arithmetic (golden-tested); the probability is "
                                        "CRISIL's historical default rate for the grade. CRISIL doesn't publish the "
                                        "sample behind each average here, so no n is shown.")  # fmt: skip
    return Signal(action=action, score=round(score, 1), event=event, horizon=horizon, method=method,
                  validation=validation, probability=round(p, 4),
                  probability_interval=(round(interval[0], 4), round(interval[1], 4)),
                  base_rate=None,
                  factors=factors, caveats=caveats, **common)  # fmt: skip


@register("bond")
async def bond_signal(instrument: str, ctx: dict[str, Any]) -> Signal:
    from finresearch.api.markets import ISIN_RE

    code = instrument.strip().upper()
    if not ISIN_RE.match(code):
        raise ValueError(f"{instrument!r} is not an ISIN")
    rows = [x for x in await _bonds() if x.isin.upper() == code]
    if not rows:
        raise LookupError(f"{code} is not in NSE's list of traded bonds")
    bond = max(rows, key=lambda x: x.traded_value or 0)
    verified = None if ctx.get("freq") else await _verified_freq(code)
    return assess(bond, await _par_curve(), await _profile(), ctx, SOURCES.today(), verified)


__all__ = [
    "LADDER",
    "SOURCES",
    "BondSources",
    "add_years",
    "assess",
    "bond_signal",
    "fd_rate_for",
    "holding_period_yield",
]  # fmt: skip
