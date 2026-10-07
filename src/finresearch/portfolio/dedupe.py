"""Cross-source reconciliation (#236): is an incoming trade already in the portfolio from another source?

Every import path calls `check` before it writes: tradebook and CAS uploads, the inbox folder and API syncs (through
service.add_txns), and manual entries (service.manual_txn). The rules are documented in connectors/merge.py and
docs/BROKER_SETUP.md; in short, for each (instrument, account, day, side) the incoming rows are compared with rows
from *other* sources (same-source re-imports are already caught by `dedupe_key`):

1. the same exchange trade id, or the same order id when the totals agree → already present;
2. two rows that both carry an exchange trade id (Zerodha, Dhan) but different ones are different trades;
3. the same quantity at a price within `PRICE_TOL` → already present (one row each, so two real buys of 10 against one
   stored buy of 10 leave one new);
4. what is left on both sides adding up to the same units at the same average price (fills vs one order) → present;
5. anything else left on both sides is a partial overlap: a **conflict**, reported and not added.

Candidates are rows of the same instrument (ISIN, symbol, scheme code, BSE code) in the same account, plus manual
entries in any account (a manual buy is often kept under "Manual"). Two broker accounts are never matched: the same
buy in two demat accounts is two trades. Nothing is ever edited or deleted here.

Mutual funds: a broker's fund baseline (Kite Coin, an `opening` at its average cost, connectors.merge rule 5) and a CAS
describe the same units. When a CAS brings history that covers the baseline's units on its day, the baseline is
*superseded*: it stays stored, `rebuild` marks it ``meta.superseded_by`` and the lots, XIRR and history ignore it
(lots.superseded_openings), so deleting the CAS import brings it back. A CAS that covers fewer units is a conflict.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from finresearch.db.models import PortfolioHolding, PortfolioTxn
from finresearch.portfolio.importers import ImportedTxn

UNITS_TOL = Decimal("0.001")
PRICE_TOL = Decimal(
    "0.005"
)  # 0.5 %: a CSV's Value/Quantity, a typed price or an order's average vs a fill price
TRADE_KINDS = ("buy", "sell")
# sources whose `trade_id` is the exchange's own trade id, so two different values are two different trades. Dhan:
# the API field is exchangeTradeId. Zerodha: Kite /trades `trade_id` and the Console tradebook's `trade_id` are taken
# to be the exchange trade id [U: not stated in the docs read for connectors/zerodha.py; a shared value still matches
# either way]. Groww's API ids are Groww's own order ids and Upstox's are unverified: there only a shared value counts.
EXCHANGE_TRADE_ID = {"zerodha", "zerodha_api", "dhan_api"}
ID_FIELDS = ("trade_id", "order_id", "trade_num")


@dataclass
class Check:
    new: list[ImportedTxn] = field(default_factory=list)
    cross_source: list[dict[str, Any]] = field(default_factory=list)  # skipped: another source has it
    conflicts: list[dict[str, Any]] = field(default_factory=list)  # partial overlaps: not added, review
    status: dict[int, str] = field(
        default_factory=dict
    )  # id(txn) -> "new" | "already present from X" | "conflict"


def instrument_holdings(s: Session, t: ImportedTxn) -> list[PortfolioHolding]:
    """Every holding of `t`'s instrument, in any account: same key or any shared identifier."""
    ids = [(col, v) for col, v in ((PortfolioHolding.isin, t.isin), (PortfolioHolding.nse_symbol, t.nse_symbol),
                                   (PortfolioHolding.bse_code, t.bse_code),
                                   (PortfolioHolding.scheme_code, t.scheme_code)) if v]  # fmt: skip
    conds = [PortfolioHolding.ikey == t.ikey, *[col == v for col, v in ids]]
    return list(s.scalars(select(PortfolioHolding).where(or_(*conds)).order_by(PortfolioHolding.id)))


def _price(q: Decimal | None, price: Decimal | None, amount: Decimal | None) -> Decimal | None:
    if price is not None:
        return price
    return abs(amount) / q if amount is not None and q else None


def _close(a: Decimal | None, b: Decimal | None) -> bool:
    """Prices within PRICE_TOL. An unknown price never matches: unknown is not proof of the same trade."""
    if a is None or b is None:
        return False
    return abs(a - b) <= PRICE_TOL * max(abs(a), abs(b), Decimal("0.000001"))


@dataclass
class _Row:
    """One side of a comparison: an incoming ImportedTxn or a stored PortfolioTxn."""

    obj: Any
    source: str
    account: str
    quantity: Decimal
    price: Decimal | None
    ids: dict[str, str]

    def exchange_trade_id(self) -> str | None:
        return self.ids.get("trade_id") if self.source in EXCHANGE_TRADE_ID else None


def _ids(meta: dict[str, Any] | None) -> dict[str, str]:
    return {k: str(v) for k in ID_FIELDS if (v := (meta or {}).get(k)) not in (None, "")}


def _distinct(a: _Row, b: _Row) -> bool:
    x, y = a.exchange_trade_id(), b.exchange_trade_id()
    return bool(x and y and x != y)


def _total(rows: Iterable[_Row]) -> tuple[Decimal, Decimal | None]:
    rows = list(rows)
    q = sum((r.quantity for r in rows), Decimal(0))
    if not q or any(r.price is None for r in rows):
        return q, None
    return q, sum((r.quantity * r.price for r in rows), Decimal(0)) / q  # type: ignore[operator]


def _label(sources: Iterable[str]) -> str:
    return "already present from " + ", ".join(sorted(set(sources)))


def _match_group(
    inc: list[_Row], old: list[_Row]
) -> tuple[list[tuple[_Row, list[_Row], str]], list[_Row], list[_Row]]:
    """Pair incoming rows with stored ones (rules 1-4). Returns (matched [(incoming, stored, how)], incoming left
    unmatched, stored rows left that could still be the same trades)."""
    pairs: list[tuple[_Row, list[_Row], str]] = []
    inc, old = list(inc), list(old)
    used: list[_Row] = []
    # 1a. the same trade id
    for r in list(inc):
        hit = next(
            (o for o in old if r.ids.get("trade_id") and o.ids.get("trade_id") == r.ids["trade_id"]), None
        )
        if hit is not None:
            pairs.append((r, [hit], "order or trade id"))
            inc.remove(r)
            old.remove(hit)
            used.append(hit)
    # 1b. the same order id, totals agreeing (fills on one side, the order on the other)
    for oid in sorted({r.ids["order_id"] for r in inc if r.ids.get("order_id")}):
        mine = [r for r in inc if r.ids.get("order_id") == oid]
        theirs = [o for o in old if o.ids.get("order_id") == oid]
        if not theirs:
            continue
        (q1, p1), (q2, p2) = _total(mine), _total(theirs)
        if abs(q1 - q2) <= UNITS_TOL:
            pairs += [(r, theirs, "order or trade id") for r in mine]
            inc = [r for r in inc if r not in mine]
            old = [o for o in old if o not in theirs]
    # 2. exchange trade ids that differ: those stored rows are other trades
    left: list[_Row] = []
    for r in inc:
        cands = [o for o in old if not _distinct(r, o)]
        # 3. one row each: the same quantity at (about) the same price
        hit = next(
            (o for o in cands if abs(o.quantity - r.quantity) <= UNITS_TOL and _close(o.price, r.price)), None
        )
        if hit is not None:
            pairs.append((r, [hit], "same day, side, quantity and price"))
            old.remove(hit)
        else:
            left.append(r)
    rest = [o for o in old if any(not _distinct(r, o) for r in left)]
    if left and rest:  # 4. what is left adds up on both sides
        (q1, p1), (q2, p2) = _total(left), _total(rest)
        if abs(q1 - q2) <= UNITS_TOL and _close(p1, p2):
            pairs += [(r, rest, "same day, side and total units") for r in left]
            return pairs, [], []
    return pairs, left, rest


def check(s: Session, txns: list[ImportedTxn]) -> Check:
    """Split `txns` into new rows, rows already present from another source, and conflicts (see the module doc).
    Only buys and sells are compared; other kinds pass through as new."""
    out = Check()
    groups: dict[tuple[str, str, date, str, str], list[ImportedTxn]] = defaultdict(list)
    for t in txns:
        if t.kind in TRADE_KINDS and t.quantity:
            groups[(t.ikey, t.account, t.day, t.kind, t.source)].append(t)
        else:
            out.new.append(t)
            out.status[id(t)] = "new"
    hcache: dict[str, list[PortfolioHolding]] = {}
    for (ikey, account, day, kind, source), rows in groups.items():
        hs = hcache.get(ikey)
        if hs is None:
            hs = hcache[ikey] = instrument_holdings(s, rows[0])
        acc = {h.id: h.account for h in hs}
        stored = s.scalars(select(PortfolioTxn).where(PortfolioTxn.holding_id.in_(list(acc)), PortfolioTxn.day == day,
                                                      PortfolioTxn.kind == kind, PortfolioTxn.source != source)
                           .order_by(PortfolioTxn.id)).all() if acc else []  # fmt: skip
        old = [_Row(o, o.source, acc[o.holding_id], o.quantity, _price(o.quantity, o.price, o.amount), _ids(o.meta))
               for o in stored if o.quantity
               and (acc[o.holding_id] == account or o.source == "manual" or source == "manual")]  # fmt: skip
        inc = [_Row(t, t.source, t.account, t.quantity, _price(t.quantity, t.price, t.amount), _ids(t.meta))
               for t in rows]  # type: ignore[arg-type]  # fmt: skip
        pairs, left, rest = _match_group(inc, old) if old else ([], inc, [])
        for r, theirs, how in pairs:
            t = r.obj
            label = _label(o.source for o in theirs)
            out.status[id(t)] = label
            out.cross_source.append({"name": t.name, "account": account, "day": day.isoformat(), "kind": kind,
                                     "units": _s(t.quantity), "matched": how,
                                     "sources": sorted({o.source for o in theirs}),
                                     "accounts": sorted({o.account for o in theirs}), "label": label})  # fmt: skip
        if left and rest:
            q1, _ = _total(left)
            q2, _ = _total(rest)
            out.conflicts.append({"name": left[0].obj.name, "account": account, "day": day.isoformat(), "kind": kind,
                                  "units": _s(q1), "other_units": _s(q2), "sources": sorted({o.source for o in rest}),
                                  "accounts": sorted({o.account for o in rest}),
                                  "why": f"{_label(o.source for o in rest)} with different units or price on this "
                                         "day: not added, review and enter the difference by hand"})  # fmt: skip
            for r in left:
                out.status[id(r.obj)] = "conflict"
        else:
            for r in left:
                out.new.append(r.obj)
                out.status[id(r.obj)] = "new"
    order = {id(t): i for i, t in enumerate(txns)}
    out.new.sort(key=lambda t: order[id(t)])
    return out


def _s(v: Decimal | None) -> str | None:
    return None if v is None else format(v.normalize(), "f")


# --------------------------------------------------------------------------- MF: CAS vs a broker's fund baseline
def _is_baseline(t: Any) -> bool:
    return t.kind == "opening" and bool((t.meta or {}).get("baseline"))


def _probe(h: PortfolioHolding) -> ImportedTxn:
    return ImportedTxn(account=h.account, asset_type=h.asset_type, name=h.name, day=date.min, kind="opening",
                       isin=h.isin, nse_symbol=h.nse_symbol, bse_code=h.bse_code, scheme_code=h.scheme_code)  # fmt: skip


def cas_units_on(s: Session, h: PortfolioHolding, day: date, extra: Iterable[ImportedTxn] = ()) -> tuple[Decimal, list[str]]:  # fmt: skip
    """Units of `h`'s fund that CAS rows in *other* accounts hold at the end of `day` (stored rows plus `extra`, an
    import not written yet), and those accounts."""
    from finresearch.portfolio.lots import Event, build_lots

    by_acc: dict[str, list[Event]] = defaultdict(list)
    for x in instrument_holdings(s, _probe(h)):
        if x.account == h.account:
            continue
        for t in s.scalars(select(PortfolioTxn).where(PortfolioTxn.holding_id == x.id, PortfolioTxn.source == "cas",
                                                      PortfolioTxn.day <= day)):  # fmt: skip
            by_acc[x.account].append(Event(t.id, t.day, t.kind, t.quantity, t.price, t.amount, t.charges or Decimal(0),
                                           t.stt_paid, dict(t.meta or {})))  # fmt: skip
    for t in extra:
        if t.source == "cas" and t.account != h.account and t.day <= day:
            by_acc[t.account].append(Event(None, t.day, t.kind, t.quantity, t.price, t.amount, t.charges, t.stt_paid,
                                           dict(t.meta)))  # fmt: skip
    units = sum((build_lots(evs).units for evs in by_acc.values()), Decimal(0))
    return units, sorted(a for a, evs in by_acc.items() if evs)


def superseded_baselines(s: Session, h: PortfolioHolding, txns: Iterable[PortfolioTxn]) -> dict[int, str]:
    """{txn id: CAS accounts} for a fund's broker baselines that CAS history covers (module doc). Used by rebuild."""
    if h.asset_type != "mf":
        return {}
    out = {}
    for t in txns:
        if _is_baseline(t) and t.quantity:
            units, accs = cas_units_on(s, h, t.day)
            if accs and units >= t.quantity - UNITS_TOL:
                out[t.id] = ", ".join(accs)
    return out


def check_fund_baselines(s: Session, txns: list[ImportedTxn], chk: Check) -> list[dict[str, Any]]:
    """For a CAS import: the broker fund baselines its rows will supersede (returned), or a conflict in `chk` when the
    CAS covers fewer units than the baseline. The CAS rows are added either way: they are the fund's real history."""
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for t in txns:
        if t.asset_type != "mf" or t.source != "cas":
            continue
        for h in instrument_holdings(s, t):
            if h.id in seen or h.account == t.account:
                continue
            seen.add(h.id)
            for b in s.scalars(select(PortfolioTxn).where(PortfolioTxn.holding_id == h.id)):
                if not _is_baseline(b) or not b.quantity or (b.meta or {}).get("superseded_by"):
                    continue
                units, accs = cas_units_on(s, h, b.day, txns)
                row = {"name": h.name, "account": h.account, "day": b.day.isoformat(), "units": _s(b.quantity),
                       "cas_units": _s(units), "sources": [b.source], "cas_accounts": accs}  # fmt: skip
                if units >= b.quantity - UNITS_TOL:
                    out.append(
                        {**row, "label": f"{_label([b.source])} (fund baseline): the CAS history replaces it"}
                    )
                else:
                    chk.conflicts.append({**row, "kind": "opening", "other_units": _s(b.quantity),
                                          "why": f"{_label([b.source])} as a fund baseline of {_s(b.quantity)} units "
                                                 f"in {h.account}, but the CAS holds {_s(units)} on that day: both "
                                                 "are kept, review (delete the baseline import if the CAS is "
                                                 "right)"})  # fmt: skip
    return out


def fund_baseline_holdings(s: Session, holdings: Iterable[PortfolioHolding]) -> set[int]:
    """Holdings with a broker fund baseline for the same funds as `holdings` (to rebuild after a CAS change)."""
    out: set[int] = set()
    for h in holdings:
        if h.asset_type != "mf":
            continue
        for x in instrument_holdings(s, _probe(h)):
            if x.id != h.id and any(_is_baseline(t) for t in s.scalars(select(PortfolioTxn)
                                                                        .where(PortfolioTxn.holding_id == x.id))):  # fmt: skip
                out.add(x.id)
    return out
