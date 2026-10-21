"""Capital-gains tax on the portfolio: tax class per holding, realised gains per financial year with the ₹1.25 lakh
exemption meter, tax-loss and gain-harvesting suggestions, and a CSV for a chartered accountant.

All tax arithmetic is fincalc.tax's dated rule table; this module maps holdings and lots onto it. Everything is a
personal estimate (fincalc.tax.VERIFY_NOTE).

Harvesting, in short (research notes 30-Sep-2026):
- India has **no wash-sale rule**: selling to book a loss (or a gain inside the exemption) and buying back is allowed.
  The rebuy resets the cost and the 12-month clock.
- Sell and rebuy on **different days**: the same stock bought and sold on one day in one account is an intraday
  trade (speculative business income), not a capital gain.
- Costs: STT 0.1 % on each side of a delivery trade (verified, Budget 2024 memo), stamp duty on the buy, brokerage,
  exchange and DP charges, the bid-ask spread and a day out of the market; funds: exit load (check the scheme) and a
  NAV gap.
- Avoid the s.94(7)/(8) dividend- and bonus-stripping windows (a loss bought around a record date is disallowed), and
  the sham-transaction doctrine is a (small) litigation risk. GAAR needs a tax benefit above ₹3 crore.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from finresearch.fincalc.dates import fiscal_year
from finresearch.fincalc.losses import Ledger, ManualLoss, YearGains, carry_forward, due_date, last_fy
from finresearch.fincalc.tax import (
    CESS,
    GRANDFATHER_DATE,
    SGB_SUBSCRIBER_UNKNOWN,
    VERIFY_NOTE,
    BroughtForward,
    Classification,
    FyTax,
    Gain,
    classify,
    fy_label,
    fy_tax,
    unverified_note,
)

STT_DELIVERY = Decimal("0.001")  # each side of a delivery equity trade (Budget 2024 memo p.46) [V]
STAMP_DELIVERY_BUY = Decimal("0.00015")  # 0.015 % on the buy (Indian Stamp Act schedule via broker pages) [S]
STAMP_MF_BUY = Decimal("0.00005")  # 0.005 % on MF purchases [S]
STCG_EQUITY = Decimal("0.20")  # s.111A / s.196 from 23-Jul-2024 (fincalc.tax rule equity-2024)
LTCG_RATE = Decimal("0.125")  # s.112 / s.112A (s.197 / s.198) from 23-Jul-2024
# GAAR and substance (research note 21-Oct-2026). Chapter X-A of the Income-tax Act 1961 (ss.95-102, in force from
# AY 2018-19) lets the assessing officer deny the tax benefit of an "impermissible avoidance arrangement" whose main
# purpose is a tax benefit and which lacks commercial substance (s.97); Rule 10U(1)(a) keeps it off arrangements whose
# tax benefit in the year is ₹3 crore or less. Its section numbers in the Income-tax Act 2025 were not verified [U].
# Below that threshold the judicial substance-over-form doctrine (McDowell & Co. v CTO, Supreme Court, 1985, as read in
# Azadi Bachao Andolan, 2003) can still treat a colourable device as a sham.
LOSS_LAW = (
    "Within a year a short-term capital loss is set off against any capital gain and a long-term one only against "
    "long-term gains (s.70 of the 1961 Act, s.108 of the 2025 Act). What is left is carried forward for eight years "
    "with the same restriction (s.74 / s.111), only if that year's return was filed by the s.139(1) due date (s.80 / "
    "s.121), and is set off before the ₹1.25 lakh LTCG exemption. 2025 Act numbers from a secondary source (21-Oct-2026)."
)
GAAR_CAUTION = (
    "No wash-sale rule does not mean no scrutiny: a sale made only to book a loss and bought straight back can be "
    "questioned for lack of substance. GAAR (Chapter X-A, ss.95-102 of the 1961 Act) applies only to a tax benefit "
    "above ₹3 crore in a year (Rule 10U), but below that a colourable device can still be disregarded (McDowell, "
    "1985). Keep it a genuine trade: a later trading day, real market exposure in between, and your contract notes."
)
HARVEST_NOTES = [
    "India has no wash-sale rule: you may sell and buy back the same security. The buyback resets your cost and the "
    "12-month holding clock.",
    "Buy back on a later trading day: a same-day sell and buy in one account is an intraday trade (speculative business "
    "income, s.43(5)), not a capital gain or loss, so no capital loss is booked at all.",
    GAAR_CAUTION,
    "Costs: STT 0.1 % on each side, stamp duty, brokerage and DP charges, the spread and a day out of the market; for "
    "funds, any exit load and the NAV gap.",
    "Avoid buying around a dividend or bonus record date and selling at a loss soon after: s.94(7)/(8) disallow such "
    "losses.",
]


# --------------------------------------------------------------------------- tax class of a holding
EQUITY_CATEGORY = ("equity scheme", "elss", "aggressive hybrid", "arbitrage", "equity savings")
DEBT_CATEGORY = ("debt scheme", "conservative hybrid", "liquid", "overnight", "money market", "gilt", "credit risk",
                 "banking and psu", "corporate bond", "duration", "floater", "dynamic bond")  # fmt: skip
OTHER_WORDS = ("gold", "silver", "overseas", "international", "global", "nasdaq", "s&p 500", "us equity", "fund of fund",
               "fof")  # fmt: skip
DEBT_WORDS = ("liquid", "gilt", "g-sec", "gsec", "sdl", "bond", "debt", "crisil", "money market", "overnight",
              "target maturity", "psu", "treasury", "t-bill", "duration", "credit risk", "floater", "income fund",
              "arbitrage")  # fmt: skip
EQUITY_NAME_WORDS = ("flexi cap", "large cap", "mid cap", "small cap", "multi cap", "large & mid", "elss", "tax saver",
                     "focused", "contra", "dividend yield", "value fund", "equity")  # fmt: skip
EQUITY_INDEX_WORDS = ("nifty", "sensex", "bse ", "midcap", "smallcap", "next 50", "bank", "momentum", "quality",
                      "value", "alpha", "equity")  # fmt: skip


# A debenture or bond held as a listed security (an NCD bought on the exchange arrives as a "stock" holding). Indian
# ISINs carry the security type in characters 8-9: "01" equity shares, "07"/"08" debentures and bonds (observed on
# exchange-listed NCDs and bonds, e.g. INE027E07998, INE549K07IQ3) [unverified against NSDL's code list]; the name
# catches the rest. ETFs are excluded first ("Bharat Bond ETF" is a fund unit).
_DEBT_NAME = re.compile(r"\bNCDs?\b|-NCD\b|\bdebentures?\b|\bbonds?\b|\bnon[- ]convertible\b", re.I)


# Government securities (#294). Indian ISINs of Central Government securities start "IN00" (dated G-secs, T-bills,
# SGBs) [S: NSDL's ISIN structure, not re-read]. Other "IN" + digit prefixes are NOT taken as government on their own:
# "IN9" is a partly paid-up share (e.g. a rights issue's partly paid shares, IN9155A01020) and must stay equity, and
# the state-code range of SDL ISINs could not be cited, so an SDL needs its name ("SDL", "state development loan") or
# a coupon in its name with an "IN" + digit 1-8 ISIN [S].
# A T-bill's ISIN has X (91-day), Y (182-day) or Z (364-day) in the ninth character, e.g. IN002025Z123 [S: observed
# on RBI/CCIL T-bill ISINs]. Names catch the rest. An SGB is a Central Government security too, but is taxed under
# its own rules (tax class "sgb"), so `sovereign_kind` names it first.
_SGB_NAME = re.compile(r"sovereign gold|\bSGB\b", re.I)
_TBILL_NAME = re.compile(r"\bT[- ]?BILLS?\b|treasury bill|\b(91|182|364)[- ]?(D|DAY|DAYS)\b", re.I)
_SDL_NAME = re.compile(r"\bSDL\b|state development loan", re.I)
_COUPON = re.compile(r"\d+(\.\d+)?\s?%")
_GSEC_NAME = re.compile(r"\bG[- ]?SEC\b|\bGOI\b|government of india|\bGS\s?20\d\d\b", re.I)


def sovereign_kind(isin: str | None, name: str | None, symbol: str | None = None) -> str | None:
    """ "sgb", "tbill", "gsec" (a Central Government dated security), "sdl" (a State Development Loan) or None."""
    n, sym, code = name or "", (symbol or "").upper(), (isin or "").upper()
    if sym.startswith("SGB") or _SGB_NAME.search(n):
        return "sgb"
    if len(code) == 12 and code.startswith("IN9"):
        return None  # a partly paid-up share, not a government security
    central = len(code) == 12 and code.startswith("IN00")
    state = len(code) == 12 and code[:2] == "IN" and code[2] in "12345678" and bool(_COUPON.search(n))
    if (central and code[8] in "XYZ") or _TBILL_NAME.search(n):
        return "tbill"
    if state or _SDL_NAME.search(n):
        return "sdl"
    if central or _GSEC_NAME.search(n):
        return "gsec"
    return None


def is_debt_security(isin: str | None, name: str | None, symbol: str | None = None) -> bool:
    """True for a listed debenture/bond (NCD) or a Government security (G-sec, SDL, T-bill) held like a share: taxed
    as an 'other' listed security, never equity. An SGB is not one here (tax class "sgb")."""
    n, sym = name or "", (symbol or "").upper()
    if (
        re.search(r"\bETF\b|\bBEES\b", n, re.I)
        or sym.endswith(("BEES", "ETF", "IETF"))
        or "sovereign gold" in n.lower()
    ):
        return False
    kind = sovereign_kind(isin, name, symbol)
    if kind == "sgb":
        return False
    if kind is not None:
        return True
    code = (isin or "").upper()
    if len(code) == 12 and code.startswith("INE") and code[7:9] in ("07", "08"):
        return True
    return bool(_DEBT_NAME.search(n))


# Why a Government security is an "other" listed security (#294; rules as on 12-Oct-2026). Dated G-secs, SDLs and
# T-bills trade on NSE and BSE and are treated as listed securities: long-term when held more than 12 months
# (s.2(42A) first proviso), LTCG at 12.5 % without indexation for transfers on or after 23-Jul-2024 (s.112 as amended
# by the Finance (No.2) Act 2024; s.197 of the 2025 Act), STCG at the slab rate; never s.111A/s.112A (no STT), so no
# ₹1.25 lakh exemption. [S: that a G-sec counts as "listed" for the 12-month rule is the common reading, not a ruling.]
# Citation (#316, decision kept): Income-tax Act 1961 s.2(42A) first proviso as amended by the Finance (No.2) Act 2024
# (fincalc.tax.FINANCE_BILL_2024): "a security (other than a unit) listed in a recognised stock exchange" is long-term
# when held more than 12 months; G-secs and SDLs are admitted to trading on NSE and BSE (NSE/BSE retail G-sec
# segments), so they are treated as listed. Golden cases g30 (G-sec) and g31 (T-bill) hold the arithmetic.
# A T-bill (91/182/364 days) is issued at a discount and redeemed at face: held to maturity it is always short-term,
# and the discount is taxed at the slab rate as a short-term capital gain here (some treat it as interest, "Income from
# other sources", also at the slab rate: same tax, different head; losses then differ).
SOVEREIGN_WHY = {
    "gsec": "Central Government security (G-sec): an 'other' listed security (long-term after 12 months, s.2(42A); "
            "LTCG 12.5 % under s.112, no s.112A exemption)",
    "sdl": "State Development Loan: an 'other' listed security (long-term after 12 months, s.2(42A); LTCG 12.5 % "
           "under s.112, no s.112A exemption)",
    "tbill": "Treasury bill: a discount instrument of at most 364 days, so its gain is short-term at the slab rate",
}  # fmt: skip


def auto_tax_class(asset_type: str, name: str, category: str | None, cas_type: str | None,
                   symbol: str | None, isin: str | None = None) -> tuple[str, str]:  # fmt: skip
    """(tax class, why) from what the app knows. The >65 % tests need the fund's holdings, which the app does not
    have, so this maps SEBI categories and names; the user can override it per holding."""
    n = f" {name.lower()} "
    sym = (symbol or "").upper()
    if asset_type == "stock":
        if sym.startswith("SGB") or "sovereign gold" in n:
            return "sgb", "Sovereign Gold Bond"
        metal = any(w in n or w.upper() in sym for w in ("gold", "silver"))
        if metal and ("etf" in n or "bees" in n or sym.endswith(("BEES", "ETF", "IETF"))):
            return "other_mf", "gold/silver ETF (a fund unit, not equity)"
        sov = sovereign_kind(isin, name, symbol)
        if sov in SOVEREIGN_WHY:
            return "other", SOVEREIGN_WHY[sov]
        if is_debt_security(isin, name, symbol):
            return (
                "other",
                "listed debenture/bond (NCD): an 'other' listed security, not equity (no STT, no s.112A)",
            )
        if sym in ("LIQUIDBEES", "LIQUIDCASE", "LIQUIDETF") or any(
            w in n for w in ("liquid", "gilt", "bharat bond")
        ):
            return "debt_mf", "debt/liquid ETF"
        return "equity", "listed share (or equity ETF)"
    if asset_type != "mf":
        return "other", "not a share or a fund"
    cat = (category or "").lower()
    if any(w in n for w in OTHER_WORDS) or "fund of funds" in cat or "fof" in cat:
        return "other_mf", "gold/international/fund-of-funds: neither equity-oriented nor debt"
    if any(c in cat for c in EQUITY_CATEGORY):
        return "equity", f"AMFI category {category}"
    if any(c in cat for c in DEBT_CATEGORY):
        return "debt_mf", f"AMFI category {category}"
    if not cat and "arbitrage" in n:
        return "equity", "arbitrage fund (by name): equity-oriented"
    if not cat and any(w in n for w in EQUITY_NAME_WORDS) and not any(w in n for w in DEBT_WORDS):
        return "equity", "equity fund (by name; no AMFI category matched)"
    if "index" in cat or "etf" in cat or not cat:
        if any(w in n for w in DEBT_WORDS):
            return "debt_mf", "debt index fund/ETF (by name)"
        if any(w in n for w in EQUITY_INDEX_WORDS):
            return "equity", "equity index fund/ETF (by name)"
    if cas_type and cas_type.upper() == "EQUITY":
        return "equity", "casparser scheme type EQUITY"
    if cas_type and cas_type.upper() == "DEBT":
        return "debt_mf", "casparser scheme type DEBT"
    return "other_mf", f"unclassified fund ({category or 'no category'}): set its tax class"


def is_listed(asset_type: str, name: str, meta: dict[str, Any] | None = None) -> bool:
    meta = meta or {}
    if "listed" in meta:
        return bool(meta["listed"])
    return asset_type == "stock" or " etf" in f" {name.lower()}"


# --------------------------------------------------------------------------- realised rows
@dataclass
class HoldingTax:
    """What the tax view needs about one holding."""

    id: int
    name: str
    account: str
    isin: str | None
    tax_class: str
    listed: bool
    fmv_2018: Decimal | None
    sgb_original: bool | None = None  # subscribed at issue (holding setting): True / False / None not known
    # unresolved unsupported corporate actions (#237): (ex-date, reason). A sale on or after the ex-date cannot be
    # classified until the user enters the cost allocation (portfolio.service.record_unsupported)
    blocked: tuple[tuple[date, str], ...] = ()
    debt: bool = False  # a bond or debenture (an unlisted one is taxed under s.50AA from 23-Jul-2024)


@dataclass
class DisposalRow:
    holding: HoldingTax
    acquired: date | None
    sold: date
    quantity: Decimal
    cost: Decimal | None  # actual
    proceeds: Decimal
    stt_paid: bool
    origin: str
    rbi_redemption: bool = False
    held_to_maturity: bool = False
    txn_id: int | None = None
    # the lot's own acquisition source (lot_subscription): True subscribed at issue, False an exchange purchase,
    # None the lot does not say. It wins over the holding setting (#316)
    subscribed: bool | None = None
    # computed
    cls: Classification | None = None
    tax_cost: Decimal | None = None  # after grandfathering
    gain: Decimal | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def fy(self) -> int:
        return fiscal_year(self.sold)


def redemption_flags(kind: str, meta: dict[str, Any] | None, tax_class: str) -> tuple[bool, bool]:
    """(rbi_redemption, held_to_maturity) of a disposal from its transaction (#294). A `redemption` of an SGB is a
    redemption by RBI, at maturity or early on a coupon date after the fifth year (RBI SGB FAQ, rbi.org.in
    FAQs.aspx?Id=1658, read 12-Oct-2026): exempt for an individual under s.47(viic) of the 1961 Act, which covers "any
    redemption" (early ones too), for redemptions up to 31-Mar-2026. From 1-Apr-2026 only for the original subscriber
    who held it continuously until RBI redeemed it, at maturity or early (fincalc.tax rule sgb-redemption-2026, #316):
    `held_to_maturity` is recorded but no longer decides it; `lot_subscription` and the holding setting say who
    subscribed. An exchange sale (`sell`) of an SGB is a transfer of a listed security unless its meta says RBI
    redeemed it (older manual entries)."""
    m = meta or {}
    rbi = bool(m.get("rbi_redemption")) or (kind == "redemption" and tax_class == "sgb")
    return rbi, bool(m.get("held_to_maturity"))


# Broker trade identifiers on a buy (importers and connectors.merge set them on tradebook and API trades): units
# bought with one came from the exchange, i.e. the secondary market. A statement or holdings-sync `opening` (whose
# meta.exchange only says where the security is listed) and a manual buy carry none, so they prove nothing.
TRADE_IDS = ("trade_id", "order_id", "trade_num")


def lot_subscription(kind: str | None, meta: dict[str, Any] | None) -> bool | None:
    """Whether the units of a lot were subscribed at the original issue, from the transaction that opened it (#316):
    meta.sgb_subscription (a manual entry's answer) when stated; False for an exchange trade (a buy with a broker
    trade or order id); None otherwise. No import marks an RBI-issue subscription on its own: an NSDL/CDSL e-CAS is
    reconciled as holdings only (connectors.inbox), so the holding setting is the usual way to say it."""
    m = meta or {}
    v = m.get("sgb_subscription")
    if isinstance(v, bool):
        return v
    if kind == "buy" and any(m.get(k) for k in TRADE_IDS):
        return False
    return None


def holding_subscriber(meta: dict[str, Any] | None) -> bool | None:
    """The per-holding "subscribed at issue" setting: meta.sgb_subscribed True/False, None when not set. The older
    checkbox key `sgb_original_subscriber` counts only when True: the checkbox saved False on every unticked save, so a
    stored False is not an answer and stays unknown."""
    m = meta or {}
    v = m.get("sgb_subscribed")
    if isinstance(v, bool):
        return v
    return True if m.get("sgb_original_subscriber") is True else None


def evaluate(row: DisposalRow) -> DisposalRow:
    """Classify a disposal and compute its taxable gain (grandfathering applied where it can be)."""
    h = row.holding
    if row.origin == "intraday":
        row.cls = Classification(
            "unknown", None, None, "intraday", "intraday trade: speculative business income"
        )
        row.notes.append("Intraday (same-day buy and sell): speculative business income, not capital gains")
        return row
    why = next((r for d, r in h.blocked if row.sold >= d), None)
    if why is not None:  # the cost of what was sold depends on a split the app does not model: never guessed
        row.cls = Classification("unknown", None, None, "unknown", why)
        row.notes.append(why)
        return row
    row.cls = classify(h.tax_class, row.acquired, row.sold, listed=h.listed, stt_paid=row.stt_paid,
                       rbi_redemption=row.rbi_redemption,
                       original_subscriber=row.subscribed if row.subscribed is not None else h.sgb_original,
                       held_to_maturity=row.held_to_maturity, debt=h.debt)  # fmt: skip
    if row.rbi_redemption and row.subscribed is False and h.sgb_original is True:
        row.notes.append("These units were bought on the exchange, so they are not the original subscription: the "
                         "holding's 'subscribed at issue' setting does not apply to them")  # fmt: skip
    if row.cls.reason == SGB_SUBSCRIBER_UNKNOWN:
        row.notes.append("Set 'subscribed at issue' on the holding: exempt if you subscribed when RBI issued it, "
                         "taxable if you bought it on the exchange")  # fmt: skip
    if row.cost is None:
        row.cls = Classification(
            "unknown", row.cls.rule, None, "unknown", "cost unknown: enter it to compute tax"
        )
        row.notes.append("Cost unknown (opening balance or transfer-in): enter the cost and date")
        return row
    if row.cls.term == "unknown":
        row.notes.append(f"Term unknown ({row.cls.reason}): the gain can't be classified short- or long-term, so it is "
                         "left out of the tax")  # fmt: skip
    cost = row.cost
    # s.55(2)(ac) (s.90(7) of the 2025 Act) steps the cost up only for a long-term capital asset "referred to in
    # section 112A", i.e. a transfer taxed under the equity regime. An equity holding sold without STT is classified
    # under the general rules (fincalc.tax.classify) and keeps its actual cost.
    if (row.cls.bucket == "equity_lt" and row.acquired is not None
            and row.acquired <= GRANDFATHER_DATE):  # fmt: skip
        if h.fmv_2018 is None:
            row.notes.append("Bought before 1-Feb-2018: enter the FMV on 31-Jan-2018 for grandfathering (actual cost "
                             "used, which may overstate the gain)")  # fmt: skip
        else:
            fmv_total = h.fmv_2018 * row.quantity
            cost = max(cost, min(fmv_total, row.proceeds))
            row.notes.append(
                "Grandfathered: cost = higher of actual and lower of (31-Jan-2018 FMV, sale value)"
            )
    row.tax_cost = cost
    row.gain = row.proceeds - cost if row.cls.term != "exempt" else Decimal(0)
    return row


def gains_of(rows: Iterable[DisposalRow]) -> list[Gain]:
    """Capital-gains rows for fincalc.tax.fy_tax. Intraday trades are speculative business income, not capital
    gains: they are left out here (fy_summary counts them separately) instead of being reported as disposals with an
    "unknown acquisition date or cost"."""
    return [Gain(_amount(r), r.cls, r.sold, str(r.txn_id or ""))
            for r in rows if r.cls is not None and r.origin != "intraday"]  # fmt: skip


def _amount(r: DisposalRow) -> Decimal:
    """The Gain amount fy_tax sees. An exempt transfer (an SGB redeemed by RBI) has no taxable gain (`r.gain` 0), but
    fy_tax reports the exempt gain itself (`exempt_total`), which is proceeds - cost: before #240's golden case g09
    it was always ₹0. fy_tax never taxes an exempt Gain, so this changes no tax."""
    if r.cls is not None and r.cls.term == "exempt" and r.tax_cost is not None:
        return r.proceeds - r.tax_cost
    return r.gain if r.gain is not None else Decimal(0)


def is_unclassified(r: DisposalRow) -> bool:
    """A capital-gains disposal whose term (short / long / exempt) is not known: no acquisition date, no cost, or no
    rule. Its tax cannot be computed, and it must never count as ₹0. Intraday trades are business income, not this."""
    return r.origin != "intraday" and (r.cls is None or r.cls.term == "unknown")


def unclassified(rows: Iterable[DisposalRow]) -> dict[str, Any]:
    """What could not be classified: {count, gain (the known gains among them), no_cost (count without a cost),
    no_date (count without an acquisition date), detail}. `count` 0 means complete."""
    bad = [r for r in rows if is_unclassified(r)]
    gain = sum((r.gain for r in bad if r.gain is not None), Decimal(0))
    no_cost = sum(1 for r in bad if r.cost is None)
    no_date = sum(1 for r in bad if r.acquired is None)
    actions = sorted(
        {r.cls.reason for r in bad if r.cls and r.cls.reason.startswith("unsupported corporate action")}
    )
    n_ca = sum(1 for r in bad if r.cls and r.cls.reason in actions)
    detail = ""
    if bad:
        why = [f"{n} without {w}" for n, w in ((no_date, "an acquisition date"), (no_cost, "a cost")) if n]
        why += [f"{n_ca} after an unsupported corporate action"] if n_ca else []
        detail = (f"Tax incomplete: {len(bad)} disposal(s) / ₹{float(gain):,.0f} of gains can't be classified short- "
                  f"or long-term ({'; '.join(why) or 'no rule'}), so they are not in the tax. Enter the dates and "
                  "costs for a full figure")  # fmt: skip
        if no_cost:
            detail += f" (the gain of the {no_cost} without a cost is unknown too)"
        if actions:
            detail += ". Why: " + "; ".join(actions)
    return {"count": len(bad), "gain": _f(gain), "no_cost": no_cost, "no_date": no_date, "corporate_action": n_ca,
            "detail": detail}  # fmt: skip


def rules_note(fys: Iterable[int]) -> dict[str, Any]:
    """{rules_verified, rules_note} for outputs that use the rules of the given financial years (#220)."""
    notes = [n for fy in sorted(set(fys)) if (n := unverified_note(fy)) is not None]
    return {"rules_verified": not notes, "rules_note": "; ".join(notes) or None}


@dataclass
class TaxLossInputs:
    """What only the user knows about earlier years' losses (suggest.profile.TaxLosses, stored in the profile and
    never sent to a model): whether each loss year's return was filed within s.139(1) time (absent = not said), and
    losses brought forward from years before the app's records, each with its source."""

    returns_filed_on_time: dict[int, bool] = field(default_factory=dict)
    brought_forward: list[ManualLoss] = field(default_factory=list)


def ledger_of(
    rows: Sequence[DisposalRow], slab: Decimal, inputs: TaxLossInputs | None, today: date
) -> Ledger:
    """The carry-forward ledger (fincalc.losses.carry_forward) over every year with a disposal, through the current
    financial year. A year with an unclassified disposal has an unknown unabsorbed loss."""
    inputs = inputs or TaxLossInputs()
    years: dict[int, YearGains] = {}
    for fy in sorted({r.fy for r in rows}):
        ev = [r for r in rows if r.fy == fy]
        bad = [r for r in ev if is_unclassified(r)]
        years[fy] = YearGains(gains_of(ev), complete=not bad,
                              maybe_loss=any(r.gain is None or r.gain < 0 for r in bad))  # fmt: skip
    return carry_forward(years, slab, inputs.brought_forward, inputs.returns_filed_on_time, today,
                         max([fiscal_year(today), *years]))  # fmt: skip


def _bf_block(ledger: Ledger, fy: int, t: FyTax) -> dict[str, Any]:
    y = ledger.years.get(fy)
    by_key = {e.key: e for e in ledger.entries}
    used = [{"origin_fy": e.origin_fy, "label": fy_label(e.origin_fy), "kind": e.kind, "amount": _f(e.used[fy])}
            for e in ledger.entries if fy in e.used]  # fmt: skip
    pending = [{"origin_fy": e.origin_fy, "label": fy_label(e.origin_fy), "kind": e.kind, "remaining": _f(e.remaining),
                "status": e.status} for k in (y.pending if y else []) if (e := by_key.get(k))]  # fmt: skip
    return {"set_off_short": _f(t.bf_set_off_short), "set_off_long": _f(t.bf_set_off_long), "used": used,
            "complete": y.carry_forward_complete if y else True, "pending": pending,
            "tax_if_pending_allowed": _f(y.tax_if_pending_allowed) if y else None}  # fmt: skip


def carry_forward_json(ledger: Ledger, inputs: TaxLossInputs) -> dict[str, Any]:
    """The ledger by year of origin for the tax view, plus the loss years whose filing the user has not answered."""
    entries = [{"origin_fy": e.origin_fy, "label": fy_label(e.origin_fy), "kind": e.kind, "amount": _f(e.amount),
                "remaining": _f(e.remaining), "used": {str(fy): _f(a) for fy, a in sorted(e.used.items())},
                "apply_from_fy": e.apply_from_fy, "last_fy": e.last_fy, "last_label": fy_label(e.last_fy),
                "filing": e.filing, "status": e.status, "source": e.source, "notes": e.notes}
               for e in ledger.entries]  # fmt: skip
    ask = sorted({e.origin_fy for e in ledger.entries if e.filing in ("unknown", "not_yet_due")})
    return {"entries": entries, "through_fy": ledger.through_fy, "ask_filing": ask,
            "inputs": {"returns_filed_on_time": {str(k): v for k, v in sorted(inputs.returns_filed_on_time.items())},
                       "brought_forward": [{"origin_fy": m.origin_fy, "kind": m.kind, "amount_inr": _f(m.amount),
                                            "source": m.source, "apply_from_fy": m.apply_from_fy}
                                           for m in inputs.brought_forward]},
            "law": LOSS_LAW}  # fmt: skip


def fy_summary(
    rows: Sequence[DisposalRow], fy: int, slab: Decimal, ledger: Ledger | None = None
) -> dict[str, Any]:
    """One year's figures. With `ledger` (ledger_of), losses brought forward from earlier years are set off (s.74 /
    s.111) and reported under `brought_forward`; without it they are not considered (`brought_forward` None)."""
    ev = [r for r in rows if r.fy == fy]
    y = ledger.years.get(fy) if ledger is not None else None
    t = y.tax if y is not None else fy_tax(gains_of(ev), fy, slab)
    st = sum((r.gain for r in ev if r.gain is not None and r.cls and r.cls.term == "short"), Decimal(0))
    lt = sum((r.gain for r in ev if r.gain is not None and r.cls and r.cls.term == "long"), Decimal(0))
    eq_lt = sum(
        (r.gain for r in ev if r.gain is not None and r.cls and r.cls.bucket == "equity_lt"), Decimal(0)
    )
    unk = unclassified(ev)
    complete = unk["count"] == 0
    # an unclassified gain may be short or long, equity or not, a gain or a loss: the tax on the rest is neither a
    # lower nor an upper bound of the year's tax, so it is never shown as the year's figure (#213)
    return {"fy": fy, "label": t.label, "disposals": len(ev), "stcg": _f(st), "ltcg": _f(lt), "equity_ltcg": _f(eq_lt),
            "exempt": _f(t.exempt_total), "unknown": unk["count"], "complete": complete, "unclassified": unk,
            **rules_note([fy]),
            "intraday": sum(1 for r in ev if r.origin == "intraday"),
            "exemption": {"limit": _f(t.exemption_limit), "used": _f(t.exemption_used),
                          "remaining": _f(t.exemption_remaining), "complete": complete},
            "slices": [{"bucket": s.bucket, "rate_pct": None if s.rate is None else float(s.rate * 100),
                        "slab": s.rate is None, "long": s.long, "gain": _f(s.amount), "set_off": _f(s.set_off),
                        "exempted": _f(s.exempted), "taxable": _f(s.taxable), "bf_set_off": _f(s.bf_set_off)}
                       for s in t.slices],
            "losses_carried": {"short": _f(t.losses_unabsorbed_short), "long": _f(t.losses_unabsorbed_long)},
            "brought_forward": _bf_block(ledger, fy, t) if ledger is not None else None,
            "tax": _f(t.tax) if complete else None, "cess": _f(t.cess) if complete else None,
            "total": _f(t.total) if complete else None, "total_classified": _f(t.total),
            "slab_rate_pct": float(slab * 100), "notes": t.notes}  # fmt: skip


def _f(x: Decimal | None) -> float | None:
    return None if x is None else round(float(x), 2)


# --------------------------------------------------------------------------- harvesting
@dataclass
class OpenLot:
    holding: HoldingTax
    acquired: date | None
    quantity: Decimal
    cost_per_unit: Decimal | None
    stt_paid: bool = True


def _unknown_costs(asset_type: str) -> list[str]:
    """Costs the app cannot price and leaves out of est_costs (so the net figure is before them)."""
    return ["exit load"] if asset_type == "mf" else ["brokerage"]


def _loss_why(saved: Decimal, cf_value: Decimal) -> str:
    if saved > 0 and cf_value > 0:
        return "Lowers this year's tax and leaves more loss to carry forward"
    if saved > 0:
        return "Booking this loss lowers this year's tax on gains already realised"
    return (
        "No tax saved this year: the loss (or an earlier loss it displaces) carries forward for later gains"
    )


def _round_trip_cost(tax_class: str, value: Decimal, asset_type: str) -> Decimal:
    if asset_type == "mf" and tax_class != "sgb":
        return value * (STAMP_MF_BUY + (Decimal("0.00001") if tax_class == "equity" else 0))
    return value * (STT_DELIVERY * 2 + STAMP_DELIVERY_BUY)


def _carried(t: FyTax, bf: Sequence[BroughtForward], fy: int) -> dict[str, Decimal]:
    """Losses that leave year `fy` still usable later: its own unabsorbed losses plus brought-forward losses not used
    this year whose eight years have not run out (one whose last year is `fy` lapses, s.74(2))."""
    out = {"short": t.losses_unabsorbed_short, "long": t.losses_unabsorbed_long}
    for b in bf:
        if last_fy(b.origin_fy) > fy:
            out[b.kind] += b.amount - t.bf_used.get(b.key, Decimal(0))
    return out


def harvest(rows: Sequence[DisposalRow], lots_by_holding: dict[int, list[OpenLot]], prices: dict[int, Decimal],
            asset_types: dict[int, str], today: date, slab: Decimal,
            brought_forward: Sequence[BroughtForward] = ()) -> dict[str, Any]:  # fmt: skip
    """Suggestions for the current financial year. Selling always takes a holding's oldest lots first (FIFO), so each
    suggestion sells a prefix of a holding's open lots, never a lot picked from the middle.

    - Gain harvesting: long-term equity gains up to the exemption still unused this year, booked tax-free and bought
      back at a higher cost (lower future tax).
    - Loss harvesting: sell loss-making lots; the value is the change in this year's tax (with `brought_forward`,
      the losses of earlier years usable this year, set off as fincalc.tax.fy_tax does) plus, as an upper bound, the
      tax the extra loss carried forward could save later (only if this year's return is filed on time and there are
      gains to absorb it within eight years). Ideas are ranked by that total net of the known costs."""
    fy = fiscal_year(today)
    base = [r for r in rows if r.fy == fy]
    base_gains = gains_of(base)
    now = fy_tax(base_gains, fy, slab, brought_forward)
    carried_now = _carried(now, brought_forward, fy)
    unk = unclassified(base)
    complete = unk["count"] == 0
    # with an unclassified disposal this year the unused exemption is not known (it may be equity LTCG, or a loss set
    # off first): "tax-free" gain harvesting could then be taxable, so none is suggested (#213)
    headroom = now.exemption_remaining if complete else Decimal(0)
    gain_ideas, loss_ideas = [], []
    for hid, lots in lots_by_holding.items():
        price = prices.get(hid)
        if price is None or not lots:
            continue
        h = lots[0].holding
        rows_for = []
        for lot in lots:
            if lot.cost_per_unit is None or lot.acquired is None:
                break  # FIFO would sell a lot whose cost is unknown first: stop here
            dr = evaluate(DisposalRow(h, lot.acquired, today, lot.quantity, lot.cost_per_unit * lot.quantity,
                                      price * lot.quantity, lot.stt_paid, "buy"))  # fmt: skip
            rows_for.append((lot, dr))
        if not rows_for:
            continue
        # gain harvesting: the longest prefix of long-term, in-profit equity lots, trimmed to the headroom
        if h.tax_class == "equity" and headroom > 0:
            qty, gain = Decimal(0), Decimal(0)
            for lot, dr in rows_for:
                if dr.cls is None or dr.cls.bucket != "equity_lt" or dr.gain is None or dr.gain <= 0:
                    break
                room = headroom - gain
                if room <= 0:
                    break
                per_unit = dr.gain / lot.quantity
                take = min(lot.quantity, (room / per_unit).to_integral_value(rounding="ROUND_FLOOR"))
                if take <= 0:
                    break
                qty += take
                gain += per_unit * take
                if take < lot.quantity:
                    break
            if qty > 0:
                value = qty * price
                gain_ideas.append({"holding_id": hid, "name": h.name, "account": h.account, "sell_units": _f(qty),
                                   "price": _f(price), "value": _f(value), "gain": _f(gain),
                                   "future_tax_saved_up_to": _f(gain * Decimal("0.125")),
                                   "est_costs": _f(_round_trip_cost(h.tax_class, value, asset_types.get(hid, "stock"))),
                                   "why": "Long-term gain inside this year's unused ₹1.25 lakh exemption: tax-free now; "
                                          "buying back raises your cost, so less LTCG later"})  # fmt: skip
        # loss harvesting: the prefix with the most negative total gain
        best, best_q, acc, acc_q, extra = None, Decimal(0), Decimal(0), Decimal(0), []
        chosen: list[DisposalRow] = []
        for _lot, dr in rows_for:
            if dr.gain is None:
                break
            acc += dr.gain
            acc_q += dr.quantity
            extra.append(dr)
            if acc < 0 and (best is None or acc < best):
                best, best_q, chosen = acc, acc_q, list(extra)
        if best is not None:
            after = fy_tax([*base_gains, *gains_of(chosen)], fy, slab, brought_forward)
            saved = now.total - after.total
            cf = {
                k: max(Decimal(0), v - carried_now[k])
                for k, v in _carried(after, brought_forward, fy).items()
            }
            # upper bound: a short-term loss may later absorb gains taxed at the slab or 20 % (s.111A), a long-term
            # one LTCG at 12.5 % (s.112 / s.112A, rates from 23-Jul-2024), with 4 % cess
            cf_value = (cf["short"] * max(slab, STCG_EQUITY) + cf["long"] * LTCG_RATE) * (1 + CESS)
            value = best_q * price
            atype = asset_types.get(hid, "stock")
            costs = _round_trip_cost(h.tax_class, value, atype)
            if saved + cf_value > 0:
                loss_ideas.append({"holding_id": hid, "name": h.name, "account": h.account, "sell_units": _f(best_q),
                                   "price": _f(price), "value": _f(value), "loss": _f(best),
                                   "short_term": any(r.cls and r.cls.term == "short" for r in chosen),
                                   "tax_saved": _f(saved), "estimate": not complete,
                                   "carry_forward": {"short": _f(cf["short"]), "long": _f(cf["long"])},
                                   "carry_forward_value_up_to": _f(cf_value),
                                   "net_benefit_up_to": _f(saved + cf_value - costs),
                                   "est_costs": _f(costs), "costs_unknown": _unknown_costs(atype),
                                   "why": _loss_why(saved, cf_value)})  # fmt: skip
    gain_ideas.sort(key=lambda x: -(x["gain"] or 0))
    loss_ideas.sort(key=lambda x: -(x["net_benefit_up_to"] or 0))
    fy_end = date(fy, 3, 31)
    notes = list(HARVEST_NOTES)
    notes.append(f"A loss carried forward counts only if this year's return is filed by the s.139(1) due date "
                 f"({due_date(fy + 1).strftime('%d-%b-%Y')} for an individual without an audit; s.80 / s.121), and "
                 "only against capital gains within eight years: its value is an upper bound.")  # fmt: skip
    if not complete:
        notes.insert(0, unk["detail"] + ". Gain harvesting is not suggested (the unused exemption is unknown) and the "
                     "loss-harvesting savings are estimates that leave those disposals out.")  # fmt: skip
    rn = rules_note([fy])
    if rn["rules_note"]:
        notes.insert(0, rn["rules_note"])
    return {"fy": fy, "label": fy_label(fy), "days_left": (fy_end - today).days, "deadline": fy_end.isoformat(),
            "exemption_remaining": _f(headroom) if complete else None, "tax_so_far": _f(now.total) if complete else None,
            "complete": complete, "unclassified": unk, **rn, "gain_harvest": gain_ideas[:20],
            "loss_harvest": loss_ideas[:20], "notes": notes, "verify": VERIFY_NOTE}  # fmt: skip


# --------------------------------------------------------------------------- CSV for a CA
CSV_COLUMNS = ["financial_year", "holding", "isin", "account", "tax_class", "acquired", "sold", "quantity",
               "actual_cost", "fmv_31jan2018_per_unit", "cost_for_tax", "sale_value_net", "gain", "term", "rate",
               "rule", "stt_paid", "notes"]  # fmt: skip


def export_csv(rows: Sequence[DisposalRow], fy: int | None = None) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    from finresearch.signals.base import DISCLAIMER

    w.writerow(["# Personal estimate from FinResearch's dated rule table. " + VERIFY_NOTE])
    w.writerow(["# PERSONAL – NOT FOR DISTRIBUTION. " + DISCLAIMER])
    fys = sorted({r.fy for r in rows if fy is None or r.fy == fy})
    for y in fys:  # what the CA must know before using the figures (#213, #220)
        unk = unclassified(r for r in rows if r.fy == y)
        if unk["count"]:
            w.writerow([f"# {fy_label(y)}: " + unk["detail"]])
        if (stale := unverified_note(y)) is not None:
            w.writerow([f"# {stale}"])
    w.writerow(CSV_COLUMNS)
    for r in sorted(rows, key=lambda r: (r.sold, r.holding.name)):
        if fy is not None and r.fy != fy:
            continue
        c = r.cls
        rate = "" if c is None else ("slab" if c.rate is None and c.term == "short" else
                                     ("" if c.rate is None else f"{float(c.rate * 100):g}%"))  # fmt: skip
        w.writerow([fy_label(r.fy), r.holding.name, r.holding.isin or "", r.holding.account, r.holding.tax_class,
                    r.acquired.isoformat() if r.acquired else "", r.sold.isoformat(), _plain(r.quantity),
                    _plain(r.cost), _plain(r.holding.fmv_2018), _plain(r.tax_cost), _plain(r.proceeds),
                    _plain(r.gain), c.term if c else "", rate, c.rule.id if c and c.rule else "",
                    "yes" if r.stt_paid else "no", "; ".join(r.notes)])  # fmt: skip
    return out.getvalue()


def _plain(x: Decimal | None) -> str:
    return "" if x is None else format(x.quantize(Decimal("0.01")), "f")
