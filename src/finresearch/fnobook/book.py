"""The paper book (#308): record a strategy from the builder, mark it to market, square it off or settle it at expiry,
and the book's results. The arithmetic is `fincalc.paper`; this module keeps the rows (never deleted: every trade
counts in the results, the same honesty as the forecast ledger)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import PaperLeg, PaperStrategy
from finresearch.fincalc import paper as pp
from finresearch.fincalc.dates import to_ist

# the default slippage against the LTP (a decision for the user: shown and editable on every paper trade)
DEFAULT_SLIPPAGE_PCT = 0.5
OPEN, AWAITING, CLOSED = "open", "awaiting_settlement", "closed"


@dataclass(frozen=True)
class Quote:
    """A leg's reference price now: Groww's LTP for the contract, or the option chain's last price."""

    price: float | None
    source: str
    trading_symbol: str | None = None


def _m(x: float | None) -> Decimal | None:
    return None if x is None else Decimal(str(round(x, 2)))


def _f(x: Decimal | None) -> float | None:
    return None if x is None else float(x)


def legs_of(st: PaperStrategy) -> list[pp.PaperLeg]:
    return [pp.PaperLeg(x.right, float(x.strike), x.side, x.qty, float(x.entry_price)) for x in st.legs]  # type: ignore[arg-type]


def record(s: Session, *, symbol: str, expiry: date, lot: int, legs_in: list[Any], quotes: list[Quote],
           slippage_pct: float, brokerage: float, now: datetime, spot: float | None = None, label: str = "",
           overrides: dict[str, Any] | None = None) -> PaperStrategy:  # fmt: skip
    """A new open paper strategy. `legs_in` are `signals.fno.LegIn` (right, strike, side, lots); `quotes` their
    reference prices in the same order. Raises ValueError when a leg has no price (never filled at 0)."""
    if not legs_in or len(legs_in) != len(quotes):
        raise ValueError("a paper trade needs at least one leg and a price for each")
    rows, legs = [], []
    for x, q in zip(legs_in, quotes, strict=True):
        if q.price is None or q.price <= 0:
            raise ValueError(f"no live price for {x.side} {x.right} {x.strike:g}: not recorded")
        fill = pp.fill_price(q.price, x.side, slippage_pct)
        qty = x.lots * lot
        legs.append(pp.PaperLeg(x.right, float(x.strike), x.side, qty, fill))
        rows.append(PaperLeg(right=x.right, strike=Decimal(str(x.strike)), side=x.side, lots=x.lots, qty=qty,
                             ref_price=_m(q.price), entry_price=_m(fill), trading_symbol=q.trading_symbol))  # fmt: skip
    today = to_ist(now).date()
    costs = pp.entry_costs(legs, today, brokerage, overrides)
    st = PaperStrategy(symbol=symbol.upper(), expiry=expiry, label=label[:120], status=OPEN, opened_at=now, lot_size=lot,
                       slippage_pct=Decimal(str(slippage_pct)), brokerage_per_order=Decimal(str(brokerage)),
                       spot_at_entry=_m(spot), price_source=", ".join(sorted({q.source for q in quotes}))[:80],
                       entry_costs=_m(costs.total), entry_detail=costs.to_json(), exit_detail={}, legs=rows)  # fmt: skip
    s.add(st)
    s.flush()
    return st


def mark(st: PaperStrategy, prices: dict[int, float], source: str, now: datetime) -> float | None:
    """Mark an open strategy with fresh leg prices ({leg id: price}); a leg without one keeps its last price. The
    gross MTM is None while any leg has never been priced."""
    for x in st.legs:
        p = prices.get(x.id)
        if p is not None and p > 0:
            x.last_price, x.last_price_at = _m(p), now
    gross = pp.gross_pnl(legs_of(st), [_f(x.last_price) for x in st.legs])
    if gross is not None:
        st.mtm_gross, st.mtm_at, st.mtm_source = _m(gross), now, source[:80]
    return gross


def square_off(st: PaperStrategy, prices: list[float | None], now: datetime,
               overrides: dict[str, Any] | None = None) -> None:  # fmt: skip
    """Close at `prices` (one per leg, the live price): square-off charges at those prices."""
    if st.status != OPEN:
        raise ValueError(f"paper trade {st.id} is {st.status}, not open")
    if any(p is None or p <= 0 for p in prices):
        raise ValueError("no live price for every leg: not closed")
    legs = legs_of(st)
    ps = [float(p) for p in prices]  # type: ignore[arg-type]
    costs = pp.square_off_costs(legs, ps, to_ist(now).date(), float(st.brokerage_per_order), overrides)
    gross = pp.gross_pnl(legs, ps)
    for x, p in zip(st.legs, ps, strict=True):
        x.exit_price = _m(p)
    _close(st, now, "square_off", gross, costs.total, costs.to_json())  # type: ignore[arg-type]


def settle_at_expiry(st: PaperStrategy, price: float, source: str, now: datetime,
                     overrides: dict[str, Any] | None = None) -> None:  # fmt: skip
    """Settle at the underlying's close on the expiry day (options at intrinsic value, futures at the price) with
    exercise STT on long in-the-money options."""
    out = pp.settle(legs_of(st), price, st.expiry, overrides)
    for x, v in zip(st.legs, out.final_values, strict=True):
        x.exit_price = _m(v)
    st.settlement_price, st.settlement_source = _m(price), source[:200]
    detail = {"exercise_stt": round(out.exercise_stt, 2), "itm_long_intrinsic": round(out.itm_long_intrinsic, 2),
              "note": "Settled at the underlying's close on the expiry day: options at intrinsic value, STT on "
                      "exercise for long options in the money; no other charges modelled on settlement."}  # fmt: skip
    if st.symbol not in _INDEX_SYMBOLS:
        detail["physical_settlement"] = ("Stock F&O is physically settled: an in-the-money position at expiry means "
                                         "delivery of shares, which is not modelled (settled here at intrinsic value).")  # fmt: skip
    _close(st, now, "expiry", out.gross, out.exercise_stt, detail)


_INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}


def _close(st: PaperStrategy, now: datetime, reason: str, gross: float, exit_costs: float,
           detail: dict[str, Any]) -> None:  # fmt: skip
    st.status, st.closed_at, st.close_reason = CLOSED, now, reason
    st.gross_pnl, st.exit_costs, st.exit_detail = _m(gross), _m(exit_costs), detail
    st.net_pnl = _m(gross - float(st.entry_costs) - exit_costs)


# --------------------------------------------------------------------------- views
def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def strategy_json(st: PaperStrategy, today: date) -> dict[str, Any]:
    legs = legs_of(st)
    last = [_f(x.last_price) for x in st.legs]
    net_now = None
    if st.status == OPEN and st.mtm_gross is not None and all(p is not None for p in last):
        # what closing now would realise: MTM − entry costs − square-off charges at the last prices
        close = pp.square_off_costs(legs, last, today, float(st.brokerage_per_order))  # type: ignore[arg-type]
        net_now = round(float(st.mtm_gross) - float(st.entry_costs) - close.total, 2)
    return {"id": st.id, "symbol": st.symbol, "expiry": st.expiry.isoformat(), "label": st.label, "status": st.status,
            "opened_at": _iso(st.opened_at), "days_to_expiry": (st.expiry - today).days, "lot_size": st.lot_size,
            "slippage_pct": _f(st.slippage_pct), "brokerage_per_order": _f(st.brokerage_per_order),
            "spot_at_entry": _f(st.spot_at_entry), "price_source": st.price_source,
            "net_premium": round(pp.net_premium(legs), 2), "entry_costs": _f(st.entry_costs),
            "entry_detail": st.entry_detail, "mtm_gross": _f(st.mtm_gross), "mtm_at": _iso(st.mtm_at),
            "mtm_source": st.mtm_source, "mtm_net_if_closed": net_now, "closed_at": _iso(st.closed_at),
            "close_reason": st.close_reason, "exit_costs": _f(st.exit_costs), "exit_detail": st.exit_detail,
            "settlement_price": _f(st.settlement_price), "settlement_source": st.settlement_source,
            "gross_pnl": _f(st.gross_pnl), "net_pnl": _f(st.net_pnl), "note": st.note,
            "legs": [{"id": x.id, "right": x.right, "strike": _f(x.strike), "side": x.side, "lots": x.lots, "qty": x.qty,
                      "ref_price": _f(x.ref_price), "entry_price": _f(x.entry_price),
                      "trading_symbol": x.trading_symbol, "last_price": _f(x.last_price),
                      "last_price_at": _iso(x.last_price_at), "exit_price": _f(x.exit_price)} for x in st.legs]}  # fmt: skip


def book_json(s: Session, today: date) -> dict[str, Any]:
    """Every paper trade (open, awaiting settlement, closed) and the results of the closed ones, in close order."""
    rows = list(s.scalars(select(PaperStrategy).order_by(PaperStrategy.opened_at, PaperStrategy.id)))
    closed = sorted((r for r in rows if r.status == CLOSED), key=lambda r: (r.closed_at, r.id))
    res = pp.results([float(r.net_pnl) for r in closed])  # type: ignore[arg-type]
    r2 = lambda x: None if x is None else round(x, 2)  # noqa: E731
    return {"trades": [strategy_json(r, today) for r in rows],
            "counts": {"open": sum(r.status == OPEN for r in rows), "awaiting_settlement": sum(r.status == AWAITING
                       for r in rows), "closed": len(closed), "all": len(rows)},
            "results": {"closed": res.closed, "wins": res.wins, "losses": res.losses,
                        "win_rate": None if res.win_rate is None else round(res.win_rate, 4),
                        "expectancy": r2(res.expectancy), "avg_win": r2(res.avg_win), "avg_loss": r2(res.avg_loss),
                        "total_net": round(res.total, 2), "max_drawdown": round(res.max_drawdown, 2),
                        "curve": [{"closed_at": _iso(r.closed_at), "id": r.id, "cumulative": round(c, 2)}
                                  for r, c in zip(closed, res.curve, strict=True)]},
            "method": "Every paper trade is kept: none can be deleted. Results count closed trades only, after entry "
                      "costs and exit costs (square-off charges, or exercise STT at expiry). Win rate = trades with "
                      "net P&L > 0 ÷ all closed trades; expectancy = mean net P&L per closed trade; max drawdown = "
                      "the largest fall of cumulative net P&L from its running peak, starting from 0. Fills are the "
                      "LTP moved by the trade's slippage: paper fills are kinder than real ones in thin strikes."}  # fmt: skip
