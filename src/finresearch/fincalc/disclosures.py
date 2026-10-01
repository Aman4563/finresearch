"""Arithmetic on disclosure feeds: net insider buying over a trailing window and the quarter-on-quarter change of
promoter encumbrance. Decimal throughout; None means "not computable" (never 0).

Net insider buying (SEBI PIT Regulation 7(2) disclosures). Only open-market trades in the company's equity count:
- the mode must be a market purchase or a market sale (`OPEN_MARKET`), and the side must agree with it;
- ESOP allotments, gifts, inter-se transfers between promoters, off-market transfers, pledge creation / revocation /
  invocation, bonus, rights, preferential allotments, conversions, buy-backs and "Others" are excluded and counted
  by reason, because they move no money at the market price or are not a decision to buy or sell (an ESOP exercise
  followed by a market sale shows only the sale);
- instruments other than equity (warrants, derivatives, ADRs) are excluded.
Evidence: insider purchases carry information, sales much less (Lakonishok & Lee 2001, Review of Financial Studies
14(1), "Are Insider Trades Informative?"; mostly in small firms). For Indian stocks this is context, not a signal.

The window is `days` calendar days ending today, both included, by the trade's to-date (else its from-date).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

OPEN_MARKET = {"market purchase": "buy", "market sale": "sell", "open market purchase": "buy",
               "open market sale": "sell"}  # fmt: skip
EXCLUDED_MODES = (("esop", "ESOP / employee allotment"), ("gift", "gift"), ("inter", "inter-se transfer"),
                  ("off market", "off-market transfer"), ("off-market", "off-market transfer"),
                  ("pledge", "pledge creation / release"), ("revok", "pledge creation / release"),
                  ("revoc", "pledge creation / release"), ("invoc", "pledge invocation"), ("bonus", "bonus"),
                  ("right", "rights issue"), ("preferential", "preferential allotment"), ("conversion", "conversion"),
                  ("buy back", "buy-back"), ("buyback", "buy-back"), ("transmission", "transmission"),
                  ("others", "other (as filed)"), ("other", "other (as filed)"))  # fmt: skip


def person_group(category: str | None) -> str:
    """ "promoter" (promoter / promoter group), "director_kmp" (director, key managerial personnel) or "other"
    (designated employees, immediate relatives, others)."""
    low = (category or "").lower()
    if "promoter" in low:
        return "promoter"
    if "director" in low or "key managerial" in low or "kmp" in low.split():
        return "director_kmp"
    return "other"


def classify(t: dict[str, Any]) -> tuple[str | None, str | None]:
    """(side "buy"/"sell", None) when the transaction counts, else (None, reason)."""
    inst = (t.get("instrument") or "").lower()
    if inst and "equity" not in inst and "share" not in inst:
        return None, f"instrument: {t.get('instrument')}"
    mode = " ".join((t.get("mode") or "").lower().split())
    side_mode = OPEN_MARKET.get(mode)
    if side_mode is None:
        for key, reason in EXCLUDED_MODES:
            if key in mode:
                return None, reason
        return None, f"mode: {t.get('mode') or 'not stated'}"
    side = (t.get("side") or "").strip().lower()
    if side in ("buy", "acquisition", "purchase"):
        side = "buy"
    elif side in ("sell", "sale", "disposal"):
        side = "sell"
    elif side in ("", "-"):
        side = side_mode
    if side != side_mode:
        return None, "side disagrees with mode"
    return side, None


@dataclass
class NetInsider:
    start: date
    end: date
    buy_value: Decimal = Decimal(0)
    sell_value: Decimal = Decimal(0)
    buy_qty: Decimal = Decimal(0)
    sell_qty: Decimal = Decimal(0)
    n_counted: int = 0
    by_group: dict[str, Decimal] = field(default_factory=lambda: {"promoter": Decimal(0), "director_kmp": Decimal(0),
                                                                  "other": Decimal(0)})  # fmt: skip
    excluded: Counter = field(default_factory=Counter)
    missing_value: int = 0  # counted trades without a filed value (their value is not in the totals)

    @property
    def net_value(self) -> Decimal:
        return self.buy_value - self.sell_value

    def json(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "buy_value": str(self.buy_value),
                "sell_value": str(self.sell_value), "net_value": str(self.net_value), "buy_qty": str(self.buy_qty),
                "sell_qty": str(self.sell_qty), "n_counted": self.n_counted,
                "by_group": {k: str(v) for k, v in self.by_group.items()}, "excluded": dict(self.excluded),
                "missing_value": self.missing_value}  # fmt: skip


def trade_day(t: dict[str, Any]) -> date | None:
    for k in ("to_day", "from_day", "intimated", "filed"):
        v = t.get(k)
        if v:
            return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])
    return None


def net_insider(rows: Iterable[dict[str, Any]], today: date, days: int = 90) -> NetInsider:
    """Open-market insider buying minus selling (₹, as filed) over the trailing `days`, by person group."""
    start = today - timedelta(days=days - 1)
    out = NetInsider(start=start, end=today)
    for t in rows:
        d = trade_day(t)
        if d is None or d < start or d > today:
            continue
        side, why = classify(t)
        if side is None:
            out.excluded[why or "excluded"] += 1
            continue
        qty = _dec(t.get("quantity")) or Decimal(0)
        val = _dec(t.get("value_inr"))
        if val is None:
            out.missing_value += 1
            val = Decimal(0)
        out.n_counted += 1
        g = person_group(t.get("category"))
        if side == "buy":
            out.buy_value += val
            out.buy_qty += qty
            out.by_group[g] += val
        else:
            out.sell_value += val
            out.sell_qty += qty
            out.by_group[g] -= val
    return out


def pledge_change(
    quarters: list[tuple[date, Decimal | None]],
) -> tuple[Decimal | None, date | None, date | None]:
    """(change in pp, previous quarter, latest quarter) of "% of promoter holding encumbered" between the two latest
    recorded quarters. None when fewer than two quarters with a value are recorded (the first snapshot is never a
    0 pp change)."""
    known = sorted((q, v) for q, v in quarters if q is not None and v is not None)
    if len(known) < 2:
        return None, None, known[-1][0] if known else None
    (q0, v0), (q1, v1) = known[-2], known[-1]
    return (v1 - v0).quantize(Decimal("0.0001")), q0, q1


def _dec(x: Any) -> Decimal | None:
    if x is None or x == "":
        return None
    try:
        return Decimal(str(x))
    except Exception:
        return None
