"""Capital-loss carry-forward ledger across financial years (#344). Personal estimate; verify with a CA.

The law (Income-tax Act 1961; the Income-tax Act 2025 renumbers it from tax year 2026-27 with the same substance,
mapping read on a secondary source on 21-Oct-2026 [S], PATRON_MAPPING; incometaxindia.gov.in returned HTTP 403):
- s.70(2)/(3) (s.108): within a year, a short-term capital loss is set off against any capital gain, a long-term one
  only against long-term gains. s.71(3) (s.109): a capital loss is never set off against another head.
- s.74(1) (s.111): what is left is carried forward to the following assessment years: a short-term loss against any
  capital gain, a long-term one only against long-term gains. s.74(2): for at most the eight assessment years
  immediately succeeding the year the loss was first computed. With years named by their end (fy N = FY N-1..N,
  AY N/N+1), a loss of fy N may be set off in fy N+1 .. N+8 and lapses after.
- s.80 (s.121) with s.139(3): the loss is carried forward only if it was determined on a return filed within the time
  allowed by s.139(1) (31 July of the assessment year for an individual without an audit, unless CBDT extends it). A
  belated return under s.139(4) or an updated one under s.139(8A) does not carry it.
- Losses of years before 1-Apr-2026 are set off under the 2025 Act in the manner of s.74 (the savings clause, s.536
  per the same secondary source; not read in the Act itself [U]).

Whether the loss year's return was filed on time is a fact only the user knows: never assumed. An unknown or late
filing keeps the loss out of the tax figure (lower tax is the unsafe direction); unknown is shown as unknown, with
what the tax would be if the loss is allowed. A year with an unclassified disposal has an unknown unabsorbed loss.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from finresearch.fincalc.tax import BroughtForward, FyTax, Gain, fy_tax

PATRON_MAPPING = "https://www.patronaccounting.com/blog/set-off-losses-income-tax-act-2025-section-mapping"
CARRY_FORWARD_YEARS = 8  # s.74(2) / s.111
Filing = Literal["on_time", "late", "unknown", "not_yet_due"]
Status = Literal[
    "available", "used", "lapsed", "not_carried", "unknown_filing", "not_yet_due", "unknown_amount"
]


def last_fy(origin_fy: int) -> int:
    """The last financial year in which a loss of `origin_fy` may be set off (eight assessment years after)."""
    return origin_fy + CARRY_FORWARD_YEARS


def due_date(origin_fy: int) -> date:
    """The s.139(1) due date of the loss year's return for an individual without a tax audit: 31 July of the
    assessment year (CBDT sometimes extends it; an extension is not modelled)."""
    return date(origin_fy, 7, 31)


def filing_of(origin_fy: int, filed: Mapping[int, bool], today: date | None) -> Filing:
    if origin_fy in filed:
        return "on_time" if filed[origin_fy] else "late"
    if today is not None and today <= due_date(origin_fy):
        return "not_yet_due"
    return "unknown"


@dataclass(frozen=True)
class ManualLoss:
    """A loss from the user's own return (Schedule CFL), for years before the app's records. `amount` is what is still
    unabsorbed and available from `apply_from_fy` (default: the year after the loss)."""

    origin_fy: int
    kind: Literal["short", "long"]
    amount: Decimal
    source: str
    apply_from_fy: int | None = None


@dataclass
class YearGains:
    gains: list[Gain]
    complete: bool = True  # every disposal classified
    maybe_loss: bool = False  # an unclassified disposal may be a loss (unknown cost, or a negative gain)


@dataclass
class LossEntry:
    origin_fy: int
    kind: Literal["short", "long", "unknown"]
    amount: Decimal | None  # None: not known (the origin year is incomplete)
    source: str  # "computed" or the user's source
    filing: Filing
    apply_from_fy: int
    remaining: Decimal | None = None
    used: dict[int, Decimal] = field(default_factory=dict)
    status: Status = "available"
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.remaining is None:
            self.remaining = self.amount

    @property
    def last_fy(self) -> int:
        return last_fy(self.origin_fy)

    @property
    def key(self) -> str:
        return f"{self.origin_fy}:{self.kind}"

    def open_in(self, fy: int) -> bool:
        return self.apply_from_fy <= fy <= self.last_fy and (self.remaining is None or self.remaining > 0)

    @property
    def usable(self) -> bool:
        return self.filing == "on_time" and self.amount is not None and self.kind != "unknown"


@dataclass
class YearResult:
    fy: int
    tax: FyTax  # with the brought-forward losses that are known to apply
    # False when a loss that could be set off this year has an unknown filing status or amount
    carry_forward_complete: bool
    pending: list[str]  # LossEntry.key of those losses
    # the year's tax incl. cess if the pending losses were allowed; None when an amount is unknown
    tax_if_pending_allowed: Decimal | None
    brought_forward: list[BroughtForward] = field(
        default_factory=list
    )  # what was offered to fy_tax this year


@dataclass
class Ledger:
    years: dict[int, YearResult]
    entries: list[LossEntry]  # sorted by (origin_fy, kind); statuses as at the end of `through_fy`
    through_fy: int | None


def _bf(entries: Sequence[LossEntry]) -> list[BroughtForward]:
    return [BroughtForward(e.origin_fy, e.kind, e.remaining, e.key) for e in entries]  # type: ignore[arg-type]


def _status(e: LossEntry, end: int) -> Status:
    if e.filing == "late":  # never carried, whatever the amount (s.80)
        return "not_carried"
    if e.amount is None or e.kind == "unknown":
        return "unknown_amount"
    if e.filing == "unknown":
        return "unknown_filing"
    if e.filing == "not_yet_due":
        return "not_yet_due"
    if e.remaining == 0:
        return "used"
    return "lapsed" if end > e.last_fy else "available"


def carry_forward(years: Mapping[int, YearGains], slab_rate: Decimal | float | str,
                  manual: Sequence[ManualLoss] = (), filed: Mapping[int, bool] | None = None,
                  today: date | None = None, through_fy: int | None = None) -> Ledger:  # fmt: skip
    """Walk the financial years in order: each year's tax with the losses brought forward into it (fincalc.tax.fy_tax),
    then its own unabsorbed losses enter the ledger for the next eight years. `filed` maps a loss year to whether its
    return was filed within s.139(1) time (absent: unknown). A manual entry (the user's return) replaces the app's own
    figure for the same year and kind; the app's figure is kept as a note for reconciliation."""
    filed = filed or {}
    by_key: dict[tuple[int, str], LossEntry] = {}
    for m in manual:
        start = m.apply_from_fy or m.origin_fy + 1
        if not m.origin_fy < start <= last_fy(m.origin_fy) or m.amount <= 0:
            raise ValueError(f"manual loss {m.origin_fy} {m.kind}: amount > 0, applied from FY+1 .. FY+8")
        by_key[(m.origin_fy, m.kind)] = LossEntry(
            m.origin_fy, m.kind, m.amount, m.source, filing_of(m.origin_fy, filed, today), start
        )
    entries = list(by_key.values())
    starts = [*years, *(e.apply_from_fy for e in entries)]
    out: dict[int, YearResult] = {}
    if not starts:
        return Ledger(out, [], through_fy)
    end = through_fy if through_fy is not None else max(years, default=max(starts))
    for fy in range(min(starts), end + 1):
        yg = years.get(fy, YearGains([]))
        open_ = [e for e in entries if e.open_in(fy)]
        usable = sorted((e for e in open_ if e.usable), key=lambda e: (e.origin_fy, e.kind))
        bf = _bf(usable)
        t = fy_tax(yg.gains, fy, slab_rate, bf)
        for e in usable:
            if (u := t.bf_used.get(e.key, Decimal(0))) > 0:
                e.used[fy] = u
                e.remaining = (e.remaining or Decimal(0)) - u
        room_long = sum((s.room for s in t.slices if s.long), Decimal(0))
        room_any = sum((s.room for s in t.slices), Decimal(0))
        pending = [e for e in open_ if not e.usable and e.filing != "late" and e.amount != 0
                   and (room_long if e.kind == "long" else room_any) > 0]  # fmt: skip
        tax_if = None
        if pending and all(e.amount is not None and e.kind != "unknown" for e in pending):
            tax_if = fy_tax(yg.gains, fy, slab_rate, [*bf, *_bf(pending)]).total
        out[fy] = YearResult(fy, t, not pending, [e.key for e in pending], tax_if, bf)
        filing = filing_of(fy, filed, today)
        own = (("short", t.losses_unabsorbed_short), ("long", t.losses_unabsorbed_long))
        for kind, amt in own:
            if amt <= 0:
                continue
            if (m := by_key.get((fy, kind))) is not None:
                m.notes.append(f"The app's own records give ₹{amt:,.0f}{'' if yg.complete else ' (year incomplete)'}"
                               "; the figure from your return is used")  # fmt: skip
                continue
            entries.append(LossEntry(fy, kind, amt if yg.complete else None, "computed", filing, fy + 1))  # type: ignore[arg-type]
        if not yg.complete and yg.maybe_loss:
            entries.append(LossEntry(fy, "unknown", None, "computed", filing, fy + 1))
    for e in entries:
        e.status = _status(e, end)
    order = {"short": 0, "long": 1, "unknown": 2}
    return Ledger(out, sorted(entries, key=lambda e: (e.origin_fy, order[e.kind])), end)
