"""Indian capital-gains tax on securities, as a dated rule table (personal estimate; verify with a CA).

Rules are keyed by the **transfer (trade) date** and the asset's tax class, never by section numbers: the Income-tax
Act 2025 (tax year 2026-27 onward) renumbered the sections (111A -> 196, 112 -> 197, 112A -> 198, 50AA -> 76) with
the same rates, so the rows below carry the old and new section only as a note. Research notes with sources:
scratchpad portfolio-research.md (30-Sep-2026) and docs/dev/RESEARCH_ROADMAP.md §D.7 [55]-[59].

Tax classes (``TaxClass``):
- ``equity``: listed equity shares (STT on sale; on purchase too, with the CBDT Notification 60/2018 carve-outs for
  IPO, bonus, rights, gift ...) and units of equity-oriented funds (STT on sale) — the s.111A / s.112A regime.
  An ``equity`` sale without STT falls back to ``other``.
- ``debt_mf``: specified mutual funds (> 65 % debt and money-market from 1-Apr-2025 transfers; <= 35 % domestic equity
  before). Units acquired on or after 1-Apr-2023 are short-term at the slab rate whatever the holding period
  (s.50AA / s.76); older units follow ``other``.
- ``other_mf``: fund units that are neither equity-oriented nor debt (gold ETFs/FoFs, international FoFs). They were
  specified funds under the first definition, so units acquired on or after 1-Apr-2023 and sold before 1-Apr-2025
  are slab-rate STCG; otherwise ``other`` rules apply. [S] (inference from the two definitions)
- ``sgb``: Sovereign Gold Bonds. RBI redemption is exempt for individuals; from tax year 2026-27 only for an
  original subscriber who held to maturity (Budget 2026 memo, M26 p.63; enactment not verified [U]). An exchange
  sale is an ``other`` listed security.
- ``other``: any other listed or unlisted security (``listed`` decides the holding threshold).

Holding period: long-term when held **more than** N months, counted in calendar months: bought 15-Jan-2025, sold
15-Jan-2026 is short-term; sold 16-Jan-2026 is long-term (s.2(42A) "not more than twelve months ... immediately
preceding the date of its transfer").

Not modelled (shown as caveats): surcharge (capped at 15 % on these gains), the s.87A rebate (not available against
tax at special rates), indexation for pre-23-Jul-2024 transfers of other assets, set-off of the unused basic
exemption, carry-forward of losses across years (8 years), the s.94(7)/(8) dividend and bonus-stripping rules, and
share buybacks (taxed as dividend with the cost as a capital loss from 1-Oct-2024; as capital gains from tax year
2026-27, promoters at an effective 30 %, Budget 2026 memo p.63, enactment not verified [U]). Also not modelled:
unlisted bonds and debentures transferred on or after 23-Jul-2024 are slab-rate STCG under s.50AA whatever the
holding period, which ``other`` (24-month rule) does not capture.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from finresearch.fincalc.dates import fiscal_year

TaxClass = Literal["equity", "debt_mf", "other_mf", "sgb", "other"]
TAX_CLASSES: tuple[str, ...] = ("equity", "debt_mf", "other_mf", "sgb", "other")
Term = Literal["short", "long", "exempt", "unknown"]
Status = Literal["verified", "secondary", "uncertain"]

BUDGET_2024_MEMO = "https://www.indiabudget.gov.in/budget2024-25/doc/memo.pdf"
FINANCE_BILL_2024 = "https://www.indiabudget.gov.in/budget2024-25/doc/Finance_Bill.pdf"
BUDGET_2026_MEMO = "https://www.indiabudget.gov.in/doc/memo.pdf"
BUDGET_2025_MEMO = "https://www.indiabudget.gov.in/budget2025-26/doc/memo.pdf"

JULY_23_2024 = date(2024, 7, 23)  # Finance (No.2) Act 2024: new rates for transfers on or after this day
APRIL_1_2023 = date(2023, 4, 1)  # s.50AA: specified-MF units acquired on or after this day are always STCG
APRIL_1_2025 = date(2025, 4, 1)  # second specified-MF definition (> 65 % debt) for transfers from FY 2025-26
APRIL_1_2026 = date(2026, 4, 1)  # Income-tax Act 2025 in force (tax year 2026-27); SGB exemption narrowed
GRANDFATHER_DATE = date(2018, 1, 31)  # FMV date for equity acquired before 1-Feb-2018 (s.55(2)(ac) / s.90(7))
CESS = Decimal("0.04")  # health and education cess on the tax
VERIFY_NOTE = (
    "Personal estimate from a dated rule table, before surcharge and rebates. Tax law changes and has edge cases "
    "(grandfathering, set-off order, carry-forward, residential status): verify with a chartered accountant before "
    "filing or acting on it."
)


@dataclass(frozen=True)
class TaxRule:
    id: str
    tax_class: str  # equity | other_listed | other_unlisted | specified_mf | other_mf_old | sgb_redemption
    effective_from: date  # transfer date
    effective_to: date | None  # inclusive; None = open-ended
    long_term_months: int | None  # None: no long-term treatment (deemed short-term or exempt)
    stcg_rate: Decimal | None  # None = slab rate
    ltcg_rate: Decimal | None
    ltcg_exemption: Decimal  # per financial year, across all rows sharing `exemption_pool`
    status: Status
    source: str
    note: str
    exemption_pool: str | None = None
    exempt: bool = False

    def applies(self, sold: date) -> bool:
        return self.effective_from <= sold and (self.effective_to is None or sold <= self.effective_to)

    def to_json(self) -> dict:
        return {"id": self.id, "tax_class": self.tax_class, "effective_from": self.effective_from.isoformat(),
                "effective_to": self.effective_to.isoformat() if self.effective_to else None,
                "long_term_months": self.long_term_months,
                "stcg_rate_pct": None if self.stcg_rate is None else float(self.stcg_rate * 100),
                "ltcg_rate_pct": None if self.ltcg_rate is None else float(self.ltcg_rate * 100),
                "ltcg_exemption": float(self.ltcg_exemption), "status": self.status, "source": self.source,
                "note": self.note, "exempt": self.exempt}  # fmt: skip


def _d(x: str) -> Decimal:
    return Decimal(x)


# The rule table. Rows of one tax class never overlap in time; the row whose window holds the transfer date applies.
RULES: tuple[TaxRule, ...] = (
    TaxRule("equity-2018", "equity", date(2018, 4, 1), date(2024, 7, 22), 12, _d("0.15"), _d("0.10"),
            Decimal(100_000), "verified", FINANCE_BILL_2024,
            "IT Act 1961 s.111A 15 % / s.112A 10 % above ₹1 lakh a year; transfers before 23-Jul-2024",
            exemption_pool="112A"),
    TaxRule("equity-2024", "equity", JULY_23_2024, date(2026, 3, 31), 12, _d("0.20"), _d("0.125"),
            Decimal(125_000), "verified", FINANCE_BILL_2024,
            "Finance (No.2) Act 2024: s.111A 20 % / s.112A 12.5 % above ₹1.25 lakh, for transfers on or after "
            "23-Jul-2024. In FY 2024-25 one ₹1.25 lakh limit covers the 10 % and 12.5 % gains together",
            exemption_pool="112A"),
    TaxRule("equity-2026", "equity", APRIL_1_2026, None, 12, _d("0.20"), _d("0.125"), Decimal(125_000),
            "verified", BUDGET_2026_MEMO,
            "Income-tax Act 2025 s.196 (STCG) / s.198 (LTCG above ₹1.25 lakh), tax year 2026-27 onward; same rates",
            exemption_pool="112A"),
    TaxRule("other-listed-pre2024", "other_listed", date(2018, 4, 1), date(2024, 7, 22), 12, None, _d("0.10"),
            Decimal(0), "uncertain", FINANCE_BILL_2024,
            "Listed securities other than units, before 23-Jul-2024: 12 months; LTCG 10 % without indexation or 20 % "
            "with (s.112 proviso). Shown at 10 % on the unindexed gain; listed units needed 36 months and 20 % with "
            "indexation, which is not modelled"),
    TaxRule("other-listed-2024", "other_listed", JULY_23_2024, None, 12, None, _d("0.125"), Decimal(0),
            "verified", FINANCE_BILL_2024,
            "All listed securities incl. listed units (ETFs, SGBs, bonds) from 23-Jul-2024: 12 months; LTCG 12.5 % "
            "without indexation (s.112 / s.197); STCG at the slab rate"),
    TaxRule("other-unlisted-pre2024", "other_unlisted", date(2018, 4, 1), date(2024, 7, 22), 36, None,
            _d("0.20"), Decimal(0), "uncertain", FINANCE_BILL_2024,
            "Unlisted assets and non-equity fund units before 23-Jul-2024: 36 months; LTCG 20 % WITH indexation, "
            "which is not modelled (the unindexed gain is shown)"),
    TaxRule("other-unlisted-2024", "other_unlisted", JULY_23_2024, None, 24, None, _d("0.125"), Decimal(0),
            "verified", FINANCE_BILL_2024,
            "Other assets from 23-Jul-2024: 24 months; LTCG 12.5 % without indexation; STCG at the slab rate"),
    TaxRule("specified-mf-2023", "specified_mf", APRIL_1_2023, None, None, None, None, Decimal(0), "verified",
            BUDGET_2024_MEMO,
            "s.50AA / s.76: units of a specified mutual fund acquired on or after 1-Apr-2023 are short-term at the "
            "slab rate whatever the holding period. Definition: <= 35 % domestic equity (to FY 2024-25 transfers), "
            "> 65 % debt and money-market (from FY 2025-26)"),
    TaxRule("other-mf-specified", "other_mf_new", APRIL_1_2023, date(2025, 3, 31), None, None, None, Decimal(0),
            "secondary", BUDGET_2024_MEMO,
            "Gold/international fund units acquired on or after 1-Apr-2023 met the first specified-MF definition "
            "(<= 35 % domestic equity): slab rate for transfers up to 31-Mar-2025; after that they follow the other-"
            "asset rules (inference from the two definitions; check the fund's own tax note)"),
    TaxRule("sgb-redemption", "sgb_redemption", date(2015, 11, 1), date(2026, 3, 31), None, None, None,
            Decimal(0), "secondary", BUDGET_2026_MEMO,
            "IT Act 1961 s.47(viic): redemption of SGBs by RBI is not a transfer for an individual, including a "
            "secondary-market buyer", exempt=True),
    TaxRule("sgb-redemption-2026", "sgb_redemption", APRIL_1_2026, None, None, None, None, Decimal(0),
            "uncertain", BUDGET_2026_MEMO,
            "Finance Bill 2026 (s.70(1)(x) of the 2025 Act): exempt only if subscribed at the original issue and held "
            "continuously until redemption on maturity. Enactment and the treatment of early (5-year) redemption are "
            "not verified", exempt=True),
)  # fmt: skip


def rule_for(key: str, sold: date) -> TaxRule | None:
    return next((r for r in RULES if r.tax_class == key and r.applies(sold)), None)


def add_months(d: date, months: int) -> date:
    """The same day `months` calendar months later; the 31st falls back to the month's last day."""
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise ValueError(d)


def is_long_term(acquired: date, sold: date, months: int) -> bool:
    """Held for more than `months` months: the sale is after the same calendar day `months` months later."""
    return sold > add_months(acquired, months)


def fy_label(fy: int) -> str:
    """2025 -> "FY 2024-25" (financial years are named by their end year, as fincalc.dates.fiscal_year does)."""
    return f"FY {fy - 1}-{fy % 100:02d}"


@dataclass(frozen=True)
class Classification:
    term: Term
    rule: TaxRule | None
    rate: Decimal | None  # None = slab rate (or exempt / unknown)
    bucket: str  # "equity_st", "equity_lt", "slab_st", "other_lt", "exempt", "unknown"
    reason: str
    holding_days: int | None = None


def classify(tax_class: str, acquired: date | None, sold: date, *, listed: bool = True, stt_paid: bool = True,
             rbi_redemption: bool = False, original_subscriber: bool = False,
             held_to_maturity: bool = False) -> Classification:  # fmt: skip
    """The term and rate of one disposal. `acquired` None means the acquisition date is unknown."""
    days = (sold - acquired).days if acquired else None
    if tax_class not in TAX_CLASSES:
        raise ValueError(f"unknown tax class {tax_class!r}")
    if tax_class == "sgb" and rbi_redemption:
        rule = rule_for("sgb_redemption", sold)
        if rule and (rule.id == "sgb-redemption" or (original_subscriber and held_to_maturity)):
            return Classification("exempt", rule, Decimal(0), "exempt", rule.note, days)
        tax_class = "other"
        listed = True
    if acquired is None:
        return Classification(
            "unknown", None, None, "unknown", "acquisition date unknown: enter it to classify", None
        )
    if tax_class == "equity" and not stt_paid:
        tax_class = "other"  # s.111A / s.112A need STT; otherwise the general rules apply
    if tax_class == "debt_mf" and acquired >= APRIL_1_2023:
        rule = rule_for("specified_mf", sold)
        return Classification("short", rule, None, "slab_st", rule.note if rule else "", days)
    if tax_class == "other_mf" and acquired >= APRIL_1_2023 and sold < APRIL_1_2025:
        rule = rule_for("other_mf_new", sold)
        return Classification("short", rule, None, "slab_st", rule.note if rule else "", days)
    if tax_class == "equity":
        rule = rule_for("equity", sold)
        if rule is None:
            return Classification("unknown", None, None, "unknown", f"no rule for transfers on {sold}", days)
        if is_long_term(acquired, sold, rule.long_term_months or 12):
            return Classification("long", rule, rule.ltcg_rate, "equity_lt", rule.note, days)
        return Classification("short", rule, rule.stcg_rate, "equity_st", rule.note, days)
    # before 23-Jul-2024 the 12-month rule covered listed securities "other than a unit": fund units (even listed
    # ETFs) needed 36 months like unlisted assets
    unit = tax_class in ("debt_mf", "other_mf")
    key = "other_listed" if listed and not (unit and sold < JULY_23_2024) else "other_unlisted"
    rule = rule_for(key, sold)
    if rule is None:
        return Classification("unknown", None, None, "unknown", f"no rule for transfers on {sold}", days)
    months = rule.long_term_months or 24
    if is_long_term(acquired, sold, months):
        return Classification("long", rule, rule.ltcg_rate, "other_lt", rule.note, days)
    return Classification("short", rule, None, "slab_st", rule.note, days)


def grandfathered_cost(actual_cost: Decimal, fmv_2018: Decimal | None, sale_value: Decimal) -> Decimal:
    """s.55(2)(ac) (s.90(7) in the 2025 Act): for equity acquired before 1-Feb-2018 and sold long-term, the cost is
    the higher of the actual cost and the lower of (FMV on 31-Jan-2018, sale value). All amounts for the same units."""
    if fmv_2018 is None:
        return actual_cost
    return max(actual_cost, min(fmv_2018, sale_value))


# --------------------------------------------------------------------------- one financial year
@dataclass
class Gain:
    """One realised gain or loss (a disposal) as the FY computation sees it."""

    amount: Decimal  # gain (negative = loss)
    classification: Classification
    sold: date
    ref: str = ""  # caller's id


@dataclass
class Slice:
    bucket: str
    rate: Decimal | None  # None = slab
    long: bool
    amount: Decimal
    exempted: Decimal = Decimal(0)
    set_off: Decimal = Decimal(0)

    @property
    def taxable(self) -> Decimal:
        return max(Decimal(0), self.amount - self.exempted - self.set_off)


@dataclass
class FyTax:
    fy: int
    label: str
    slab_rate: Decimal
    gross: dict[str, Decimal]  # bucket -> net gain before set-off across buckets
    slices: list[Slice]
    exemption_limit: Decimal
    exemption_used: Decimal
    losses_unabsorbed_short: Decimal
    losses_unabsorbed_long: Decimal
    exempt_total: Decimal
    unknown_count: int
    tax: Decimal
    cess: Decimal
    notes: list[str] = field(default_factory=list)

    @property
    def exemption_remaining(self) -> Decimal:
        return max(Decimal(0), self.exemption_limit - self.exemption_used)

    @property
    def total(self) -> Decimal:
        return self.tax + self.cess


def exemption_limit(fy: int) -> Decimal:
    """The s.112A / s.198 annual exemption for a financial year (named by its end year): ₹1 lakh up to FY 2023-24,
    ₹1.25 lakh from FY 2024-25 (one limit for the whole year, covering gains before and after 23-Jul-2024)."""
    return Decimal(125_000) if fy >= 2025 else Decimal(100_000)


def _rate(s: Slice, slab: Decimal) -> Decimal:
    return slab if s.rate is None else s.rate


def fy_tax(gains: Iterable[Gain], fy: int, slab_rate: Decimal | float | str = Decimal("0.30")) -> FyTax:
    """Tax on one financial year's realised capital gains.

    1. Gains and losses net within their bucket and rate (e.g. all 20 % equity STCG together).
    2. Remaining losses are set off across buckets: long-term losses only against long-term gains, short-term losses
       against any gain, each time against the highest-taxed part first (the order that minimises tax; the Act
       fixes which gains a loss may absorb, not the order among them [U]).
    3. The ₹1.25 lakh (₹1 lakh before FY 2024-25) exemption applies to the equity LTCG left, to the 12.5 % part first.
    4. Tax = rate × taxable part (slab-rate parts at `slab_rate`), plus 4 % cess. Surcharge and rebates are not modelled.
    """
    slab = Decimal(str(slab_rate))
    gl = [g for g in gains if fiscal_year(g.sold) == fy]
    exempt_total = sum((g.amount for g in gl if g.classification.term == "exempt"), Decimal(0))
    unknown = [g for g in gl if g.classification.term == "unknown"]
    net: dict[tuple[str, Decimal | None, bool], Decimal] = {}
    for g in gl:
        c = g.classification
        if c.term in ("exempt", "unknown"):
            continue
        key = (c.bucket, c.rate, c.term == "long")
        net[key] = net.get(key, Decimal(0)) + g.amount
    slices = [Slice(b, r, lg, amt) for (b, r, lg), amt in net.items() if amt > 0]
    loss_long = -sum((a for (_, _, lg), a in net.items() if a < 0 and lg), Decimal(0))
    loss_short = -sum((a for (_, _, lg), a in net.items() if a < 0 and not lg), Decimal(0))
    limit = exemption_limit(fy)

    # rank what a rupee of loss saves: for equity LTCG only the part above the exemption is taxed, so losses first
    # hit taxable parts by rate; the exemption is applied after set-off (as the Act does), highest rate first
    def order(pool: list[Slice]) -> list[Slice]:
        return sorted(pool, key=lambda s: _rate(s, slab), reverse=True)

    def absorb(loss: Decimal, pool: list[Slice]) -> Decimal:
        # first the part of each slice that would stay taxable after the exemption, then the rest
        eq_lt = [s for s in slices if s.bucket == "equity_lt"]
        eq_total = sum((s.amount - s.set_off for s in eq_lt), Decimal(0))
        shelter = min(limit, eq_total)  # equity LTCG the exemption will cover anyway
        taxable_eq = eq_total - shelter
        for s in order(pool):
            if loss <= 0:
                break
            room = s.amount - s.set_off
            if s.bucket == "equity_lt":
                take_share = min(room, max(Decimal(0), taxable_eq))
                taxable_eq -= take_share
                room = take_share
            t = min(loss, room)
            s.set_off += t
            loss -= t
        for s in order(pool):  # losses left over absorb gains the exemption would have covered
            if loss <= 0:
                break
            t = min(loss, s.amount - s.set_off)
            s.set_off += t
            loss -= t
        return loss

    loss_long = absorb(loss_long, [s for s in slices if s.long])
    loss_short = absorb(loss_short, slices)
    used = Decimal(0)
    for s in sorted(
        (s for s in slices if s.bucket == "equity_lt"), key=lambda s: _rate(s, slab), reverse=True
    ):
        t = min(limit - used, s.amount - s.set_off)
        if t > 0:
            s.exempted = t
            used += t
    tax = sum((s.taxable * _rate(s, slab) for s in slices), Decimal(0))
    notes = []
    if unknown:
        notes.append(
            f"{len(unknown)} disposal(s) have an unknown acquisition date or cost and are not in the total"
        )
    if loss_long > 0 or loss_short > 0:
        notes.append("Unabsorbed losses can be carried forward for 8 years if the return is filed on time")
    return FyTax(fy=fy, label=fy_label(fy), slab_rate=slab,
                 gross={f"{b}@{'slab' if r is None else r}": a for (b, r, _), a in net.items()}, slices=slices,
                 exemption_limit=limit, exemption_used=used, losses_unabsorbed_short=loss_short,
                 losses_unabsorbed_long=loss_long, exempt_total=exempt_total, unknown_count=len(unknown),
                 tax=tax.quantize(Decimal("0.01")), cess=(tax * CESS).quantize(Decimal("0.01")), notes=notes)  # fmt: skip


def tax_delta(
    base: Sequence[Gain], extra: Sequence[Gain], fy: int, slab_rate: Decimal | float | str
) -> Decimal:
    """How much the year's tax (incl. cess) changes if `extra` disposals are added (negative = saving)."""
    return fy_tax([*base, *extra], fy, slab_rate).total - fy_tax(base, fy, slab_rate).total
