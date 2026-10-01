"""How each registered alert metric is computed (finresearch.alerts.registry), from the app's existing polite,
cached data paths. Python computes; nothing here calls a model.

Every reading carries its source. A metric that cannot be computed returns value None with the reason (the engine
records it as "unknown" and never fires on it). Network access goes through `SOURCES` so tests run offline with
fakes; one `Reader` per evaluation pass memoises every fetch, so ten rules on one stock make one quote request.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session


@dataclass
class Reading:
    value: Decimal | None
    source: str
    as_of: str | None = None
    note: str | None = None  # why the value is unknown
    baseline: dict[str, Any] | None = None  # change metrics: what to compare the next check with
    detail: str | None = None  # e.g. "HOLD -> REDUCE" for a change flag


def unknown(reason: str, source: str = "") -> Reading:
    return Reading(None, source or reason, note=reason)


def D(x: Any) -> Decimal | None:
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except Exception:
        return None


def pct(a: Decimal, b: Decimal) -> Decimal:
    """(a / b - 1) x 100, rounded to 4 places."""
    return ((a / b - 1) * 100).quantize(Decimal("0.0001"))


# --------------------------------------------------------------------------- data sources (test seam)
@dataclass
class Sources:
    """Where readings come from; None = the live default. Each is async except `today` and `iv_series`."""

    stock_quote: Callable[[str], Awaitable[Any]] | None = None  # key ("INFY" / "BSE:500209") -> Quote
    stock_inputs: Callable[[str], Awaitable[dict[str, Any]]] | None = None  # signals.stock.inputs
    signal: Callable[[str, str], Awaitable[Any]] | None = None  # (asset, instrument) -> Signal
    fund_scheme: Callable[[str], Awaitable[Any]] | None = None  # code -> SchemeNav (NAVAll row)
    fund_navs: Callable[[Any], Awaitable[list[tuple[date, Decimal]]]] | None = None  # scheme -> last ~10 days
    fund_analyse: Callable[[str], Awaitable[dict[str, Any]]] | None = None  # signals.fund.analyse
    bonds: Callable[[], Awaitable[list]] | None = None  # NSE bond list rows (ListedBond)
    bond_freq: Callable[[str], Awaitable[int | None]] | None = None  # verified coupon frequency
    fno_expiries: Callable[[str], Awaitable[list[date]]] | None = None
    fno_chain: Callable[[str, date], Awaitable[Any]] | None = None  # (symbol, expiry) -> OptionChain
    fno_lot: Callable[[str, date], Awaitable[int | None]] | None = None
    iv_series: Callable[[str], list[tuple[date, float, float | None]]] | None = None
    today: Callable[[], date] | None = None


SOURCES = Sources()


# --------------------------------------------------------------------------- live defaults
async def _live_quote(key: str):
    from finresearch.adapters.bse_equity import scrip_code_of

    code = scrip_code_of(key)
    if code:
        from finresearch.adapters.bse_equity import BseEquity

        async with BseEquity() as eq:
            return await eq.quote(code)
    from finresearch.adapters.nse import NseClient

    async with NseClient() as nse:
        return await nse.quote(key)


async def _live_inputs(key: str) -> dict[str, Any]:
    from finresearch.signals import stock

    return await stock.inputs(key)


async def _live_signal(asset: str, instrument: str):
    from finresearch.signals import get_provider

    provider = get_provider(asset)
    if provider is None:
        raise LookupError(f"no {asset} signal provider")
    # log=0: a scheduled check must not add forecasts to the ledger (only a viewed signal does)
    return await provider(instrument, {"log": "0"})


async def _live_scheme(code: str):
    from finresearch.signals import fund

    rows = await fund._schemes()
    return next((x for x in rows if x.code == code), None)


async def _live_navs(scheme) -> list[tuple[date, Decimal]]:
    from finresearch.adapters.amfi import AmfiClient
    from finresearch.fincalc.dates import today_ist

    end = today_ist()
    async with AmfiClient() as amfi:
        rows = await amfi.scheme_history(scheme, end - timedelta(days=10), end, end)
    return sorted({r.day: r.nav for r in rows if r.day and r.nav}.items())


async def _live_analyse(code: str) -> dict[str, Any]:
    from finresearch.signals import fund

    return await fund.analyse(code)


async def _live_bonds() -> list:
    from finresearch.signals import bond

    return await bond._bonds()


async def _live_freq(isin: str) -> int | None:
    from finresearch.signals import bond

    v = await bond._verified_freq(isin)
    return v[0] if v else None


async def _live_expiries(symbol: str) -> list[date]:
    from finresearch.adapters.nse_fno import NseFno

    async with NseFno() as f:
        return (await f.contract_info(symbol))[0]


async def _live_chain(symbol: str, expiry: date):
    from finresearch.adapters.nse_fno import NseFno

    async with NseFno() as f:
        return await f.option_chain(symbol, expiry)


async def _live_lot(symbol: str, expiry: date) -> int | None:
    from finresearch.adapters.nse_fno import NseFno, lot_size_for

    async with NseFno() as f:
        return lot_size_for(await f.lot_sizes(), symbol, expiry)


def _live_iv(symbol: str):
    from finresearch.signals.fno import iv_series

    return iv_series(symbol)


def _today() -> date:
    from finresearch.fincalc.dates import today_ist

    return SOURCES.today() if SOURCES.today else today_ist()


# --------------------------------------------------------------------------- the reader
@dataclass
class Reader:
    """Computes readings for one evaluation pass, memoising every fetch (and every failure) by key."""

    session: Session
    memo: dict[Any, Any] = field(default_factory=dict)
    locks: dict[Any, asyncio.Lock] = field(default_factory=dict)

    async def _get(self, key: Any, make: Callable[[], Awaitable[Any]]) -> Any:
        if key in self.memo:
            v = self.memo[key]
            if isinstance(v, Exception):
                raise v
            return v
        try:
            v = await make()
        except Exception as e:  # remembered: a refused source is not asked again in the same pass
            self.memo[key] = e
            raise
        self.memo[key] = v
        return v

    async def read(self, kind: str, metric: str, instrument: str, params: dict[str, str],
                   baseline: dict[str, Any] | None) -> Reading:  # fmt: skip
        fn = getattr(self, f"_{kind}", None)
        if fn is None:
            return unknown(f"no metrics for {kind}")
        try:
            return await fn(metric, instrument, params, baseline or {})
        except Exception as e:  # a data source failed: unknown this time, retried on the next pass
            return unknown(f"could not read {metric} for {instrument}: {type(e).__name__}: {e}"[:300])

    # ------------------------------------------------------------------ IPO: from the monitor's own snapshots
    async def _ipo(self, metric: str, sym: str, params: dict[str, str], baseline: dict[str, Any]) -> Reading:
        from finresearch.db.models import SubscriptionSnapshotRow, Watch
        from finresearch.suggest.rules import CATEGORY_NAMES

        if metric == "bidding_days_left":
            w = self.session.scalars(
                select(Watch).where(Watch.nse_symbol == sym, Watch.kind == "ipo")
            ).first()
            if w is None or w.close_date is None:
                return unknown(f"{sym} has no IPO watch with a close date")
            from finresearch.suggest.rules import bidding_days_left

            return Reading(Decimal(bidding_days_left(_today(), w.close_date)),
                           "fincalc: exchange bidding days from today to the close")  # fmt: skip
        snap = self.session.scalars(select(SubscriptionSnapshotRow).where(SubscriptionSnapshotRow.nse_symbol == sym)
                                    .order_by(SubscriptionSnapshotRow.as_of.desc())).first()  # fmt: skip
        if snap is None:
            return unknown(f"no subscription snapshot for {sym} yet (the monitor records one per check)")
        as_of = snap.as_of.isoformat() if snap.as_of else None
        label = "BSE SME subscription" if snap.source == "bse_sme" else "NSE combined subscription"
        if metric == "total_times":
            return Reading(D(snap.total_times), f"{label} (monitor snapshot)", as_of)
        names = CATEGORY_NAMES.get(metric)
        if not names:
            return unknown(f"unknown IPO metric {metric}")
        top = [c for c in snap.categories or [] if c.get("code") and re.fullmatch(r"\d+", str(c["code"]))]
        row = next((c for c in top if any(n in str(c.get("name", "")).lower() for n in names)), None)
        if row is None or row.get("times") is None:
            return unknown(f"{label} has no {metric.split('_')[0].upper()} category times for {sym}", label)
        return Reading(D(row["times"]), f"{label} (monitor snapshot)", as_of)

    # ------------------------------------------------------------------ listed stocks
    async def _quote(self, key: str):
        q = await self._get(("quote", key), lambda: (SOURCES.stock_quote or _live_quote)(key))
        if q is None:
            raise LookupError(f"no quote for {key}")
        return q

    async def _inputs(self, key: str) -> dict[str, Any]:
        return await self._get(("inputs", key), lambda: (SOURCES.stock_inputs or _live_inputs)(key))

    async def _signal(self, asset: str, inst: str):
        return await self._get(("signal", asset, inst), lambda: (SOURCES.signal or _live_signal)(asset, inst))

    async def _change(self, asset: str, inst: str, baseline: dict[str, Any], field_: str, value: str | None,
                      source: str) -> Reading:  # fmt: skip
        """A 0/1 change flag against the last check's value (the first check only records it)."""
        if value is None:
            return unknown(f"no {field_} for {inst}", source)
        before = baseline.get(field_)
        changed = before is not None and before != value
        return Reading(Decimal(int(changed)), source, baseline={field_: value},
                       detail=f"{before} -> {value}" if changed else None)  # fmt: skip

    async def _stock(
        self, metric: str, key: str, params: dict[str, str], baseline: dict[str, Any]
    ) -> Reading:
        exch = "BSE quote" if key.startswith("BSE:") else "NSE quote"
        if metric in ("price", "day_change_pct", "pct_from_52w_high", "pct_from_52w_low"):
            from finresearch.fincalc.price import REF_LABELS, price_view

            q = await self._quote(key)
            v = price_view(q, exchange="BSE" if key.startswith("BSE:") else "NSE")
            price = D(v.price)
            as_of = q.as_of.isoformat() if getattr(q, "as_of", None) else None
            if price is None:
                return unknown(f"the {exch} has no price for {key}", exch)
            what_px = v.label.lower()
            if metric == "price":
                return Reading(price, f"{exch}: {what_px}", as_of)
            ref = {"day_change_pct": v.reference, "pct_from_52w_high": q.week52_high,
                   "pct_from_52w_low": q.week52_low}[metric]  # fmt: skip
            ref = D(ref)
            if not ref:
                return unknown(f"the {exch} has no {metric.replace('_', ' ')} reference for {key}", exch)
            what = {"day_change_pct": REF_LABELS.get(v.reference_kind or "", "previous close"),
                    "pct_from_52w_high": "52-week high", "pct_from_52w_low": "52-week low"}[metric]  # fmt: skip
            return Reading(pct(price, ref), f"{exch}: {what_px} {price} vs {what} {ref}", as_of)
        if metric in ("signal_action_changed", "signal_score"):
            sig = await self._signal("stock", key)
            if metric == "signal_score":
                return Reading(D(round(sig.score, 1)), "signals.stock composite v1 score")
            return await self._change("stock", key, baseline, "action", sig.action, "signals.stock action")
        raw = await self._inputs(key)
        today = _today()
        if metric == "pct_vs_200dma":
            from finresearch.signals.stock import features

            f = features(raw)
            if f.trend_distance is None:
                return unknown(f"fewer than 200 daily closes for {key}", "NSE daily history")
            return Reading(D(round(f.trend_distance * 100, 4)), f"price {f.price:.2f} vs 200-DMA {f.sma200:.2f} "
                           "(split-adjusted daily closes)", f.last_day.isoformat() if f.last_day else None)  # fmt: skip
        if metric == "forensic_red_flags":
            from finresearch.signals.stock import forensic

            fz = forensic(raw)
            if not fz.get("fiscal_year_end"):
                return unknown(f"no annual results XBRL for {key}", "annual Integrated Filing XBRL")
            flagged = [s["name"] for s in fz["scores"] if s.get("red_flag")]
            return Reading(Decimal(fz["red_flags"]), f"forensic scorecard, fiscal year to {fz['fiscal_year_end']}",
                           detail=", ".join(flagged) or None)  # fmt: skip
        if metric == "days_to_ex_date":
            ahead = sorted(
                (a.ex_date, a.subject) for a in raw.get("actions") or [] if a.ex_date and a.ex_date >= today
            )
            if not ahead:
                return unknown(f"no upcoming ex-date for {key}", "corporate actions")
            d, subj = ahead[0]
            return Reading(Decimal((d - today).days), f"corporate action: {subj} (ex {d})", detail=subj)
        if metric == "days_since_results":
            filed = [datetime.fromisoformat(r["filed_at"]).date() for r in (raw.get("results") or {}).get("quarters")
                     or [] if r.get("filed_at")]  # fmt: skip
            if not filed:
                return unknown(f"no results filings for {key}", "results filings")
            last = max(filed)
            return Reading(Decimal((today - last).days), f"latest results filed {last}")
        if metric == "promoter_change_pp":
            qs = [q for q in (raw.get("shareholding") or {}).get("quarters") or []
                  if (q.get("groups") or {}).get("promoter") is not None]  # fmt: skip
            if len(qs) < 2:
                return unknown(f"fewer than two shareholding patterns for {key}", "shareholding XBRL")
            a, b = qs[-2], qs[-1]
            ch = D(b["groups"]["promoter"]) - D(a["groups"]["promoter"])
            return Reading(ch.quantize(Decimal("0.0001")), f"promoter holding {a['as_of']} -> {b['as_of']} "
                           "(shareholding-pattern XBRL)", b["as_of"])  # fmt: skip
        return unknown(f"unknown stock metric {metric}")

    # ------------------------------------------------------------------ mutual funds
    async def _fund(
        self, metric: str, code: str, params: dict[str, str], baseline: dict[str, Any]
    ) -> Reading:
        from finresearch.alerts.registry import AMFI_HISTORY, AMFI_NAV

        if metric in ("nav", "nav_change_pct"):
            scheme = await self._get(("scheme", code), lambda: (SOURCES.fund_scheme or _live_scheme)(code))
            if scheme is None:
                return unknown(f"scheme {code} is not in AMFI's NAV file", AMFI_NAV)
            if metric == "nav":
                if scheme.nav is None:
                    return unknown(f"AMFI publishes no NAV for {code}", AMFI_NAV)
                return Reading(
                    D(scheme.nav),
                    f"AMFI NAVAll ({scheme.day})",
                    scheme.day.isoformat() if scheme.day else None,
                )
            navs = await self._get(("navs", code), lambda: (SOURCES.fund_navs or _live_navs)(scheme))
            if len(navs) < 2:
                return unknown(f"fewer than two NAVs in the last 10 days for {code}", AMFI_HISTORY)
            (d0, v0), (d1, v1) = navs[-2], navs[-1]
            return Reading(pct(D(v1), D(v0)), f"AMFI NAV {v0} on {d0} -> {v1} on {d1}", d1.isoformat())
        if metric == "signal_action_changed":
            sig = await self._signal("fund", code)
            return await self._change("fund", code, baseline, "action", sig.action, "signals.fund action")
        a = await self._get(("analyse", code), lambda: (SOURCES.fund_analyse or _live_analyse)(code))
        if metric in ("hit_rate_3y_pct", "excess_3y_pp"):
            ws = a.get("windows_3y") or []
            if a.get("passive"):
                return unknown("an index fund or ETF: the category-median test does not apply")
            if not ws:
                return unknown(f"not enough 3-year history or peers for {code}", "signals.fund.analyse")
            if metric == "hit_rate_3y_pct":
                hit = sum(1 for w in ws if w["fund"] >= w["median"])
                return Reading((Decimal(hit) * 100 / len(ws)).quantize(Decimal("0.01")),
                               f"{hit} of {len(ws)} quarter-end 3-year windows at or above the category median",
                               ws[-1]["end"])  # fmt: skip
            w = ws[-1]
            return Reading(D(round((w["fund"] - w["median"]) * 100, 4)),
                           f"3-year window to {w['end']}: fund {w['fund']:.2%} vs category median {w['median']:.2%}",
                           w["end"])  # fmt: skip
        if metric in ("ter_pct", "ter_change_pp"):
            t = a.get("ter") or {}
            ter = D(t.get("ter"))
            if ter is None:
                return unknown(a.get("ter_error") or f"no TER for {code} in AMFI's TER file", "AMFI TER file")
            src = f"AMFI TER file ({t.get('day')})"
            if metric == "ter_pct":
                return Reading(ter, src, t.get("day"))
            before = D(baseline.get("ter"))
            if before is None:  # first check: record the baseline, report no change
                return Reading(
                    Decimal(0), src + "; baseline recorded", t.get("day"), baseline={"ter": str(ter)}
                )
            return Reading(ter - before, f"{src}: {before}% -> {ter}%", t.get("day"), baseline={"ter": str(ter)},
                           detail=f"{before}% -> {ter}%")  # fmt: skip
        return unknown(f"unknown fund metric {metric}")

    # ------------------------------------------------------------------ bonds
    async def _bond(
        self, metric: str, isin: str, params: dict[str, str], baseline: dict[str, Any]
    ) -> Reading:
        from finresearch.alerts.registry import NSE_BONDS

        if metric == "signal_action_changed":
            sig = await self._signal("bond", isin)
            return await self._change("bond", isin, baseline, "action", sig.action, "signals.bond action")
        rows = await self._get(("bonds",), lambda: (SOURCES.bonds or _live_bonds)())
        mine = [b for b in rows if b.isin.upper() == isin]
        if not mine:
            return unknown(f"{isin} is not in NSE's list of traded bonds today", NSE_BONDS)
        b = max(mine, key=lambda x: x.traded_value or 0)
        as_of = b.as_of.isoformat() if b.as_of else None
        today = _today()
        if metric == "price":
            price = b.last_price or b.close
            return Reading(D(price), "NSE bonds list: last price", as_of) if price else unknown(
                f"no trade in {isin} today", NSE_BONDS)  # fmt: skip
        if metric == "ytm_pct":
            from finresearch.fincalc import bonds as B

            price = b.last_price or b.close
            if not (price and b.coupon_pct is not None and b.maturity and b.face_value):
                return unknown(f"price, coupon, maturity or face value missing for {isin}", NSE_BONDS)
            if b.warnings and any("partly" in w for w in b.warnings):
                return unknown(b.warnings[0], NSE_BONDS)
            freq = await self._get(("freq", isin), lambda: (SOURCES.bond_freq or _live_freq)(isin))
            how = "verified in a research run" if freq else "assumed yearly"
            # NSE's CM-segment bond prices are dirty (incl. accrued interest), as the bond page and the bond signal
            # read them; taking the quote as clean understated the YTM by the accrued interest's pull
            f, coupon, face = freq or 1, Decimal(b.coupon_pct) / 100, Decimal(b.face_value)
            ai = B.accrued_interest(today, b.maturity, coupon, f, face)
            clean = Decimal(price) - ai
            try:
                y = B.ytm(clean, today, b.maturity, coupon, f, face)
            except ValueError as e:
                return unknown(f"no yield fits {isin}'s price ₹{price}: {e}", NSE_BONDS)
            return Reading((y * 100).quantize(Decimal("0.0001")), f"fincalc.bonds.ytm at the dirty price ₹{price} "
                           f"less accrued ₹{ai.quantize(Decimal('0.01'))} (coupon {b.coupon_pct}%, matures "
                           f"{b.maturity}, frequency {f}/yr {how})", as_of)  # fmt: skip
        if metric == "rating_changed":
            rating = f"{b.rating} ({b.rating_agency})" if b.rating and b.rating_agency else b.rating
            return await self._change("bond", isin, baseline, "rating", rating, "NSE bonds list rating")
        if metric == "days_to_coupon":
            if not b.next_interest_date or b.next_interest_date < today:
                return unknown(f"NSE's list has no future interest date for {isin}", NSE_BONDS)
            return Reading(Decimal((b.next_interest_date - today).days), f"NSE next interest date "
                           f"{b.next_interest_date}", as_of)  # fmt: skip
        return unknown(f"unknown bond metric {metric}")

    # ------------------------------------------------------------------ F&O
    async def _expiry(self, sym: str, params: dict[str, str]) -> date:
        if params.get("expiry"):
            return date.fromisoformat(params["expiry"])
        exps = await self._get(("expiries", sym), lambda: (SOURCES.fno_expiries or _live_expiries)(sym))
        ahead = sorted(e for e in exps if e >= _today())
        if not ahead:
            raise LookupError(f"no F&O expiries for {sym}")
        return ahead[0]

    async def _chain(self, sym: str, expiry: date):
        return await self._get(
            ("chain", sym, expiry), lambda: (SOURCES.fno_chain or _live_chain)(sym, expiry)
        )

    async def _fno(self, metric: str, sym: str, params: dict[str, str], baseline: dict[str, Any]) -> Reading:
        from finresearch.alerts.registry import NSE_CHAIN

        if metric in ("atm_iv", "iv_percentile"):
            series = (SOURCES.iv_series or _live_iv)(sym)
            if not series:
                return unknown(
                    f"no recorded IV for {sym} yet (the monitor records it after each close)", "iv_history"
                )
            last_day, last_iv = series[-1][0], series[-1][1]
            if metric == "atm_iv":
                return Reading(D(round(last_iv, 3)), f"iv_history ATM IV on {last_day}", last_day.isoformat())
            from finresearch.fincalc.volatility import iv_stats

            st = iv_stats([v for _, v, _ in series])
            if st.percentile is None:
                return unknown(f"IV percentile for {sym}: {st.status}", "iv_history")
            return Reading(D(round(st.percentile * 100, 2)), f"IV percentile over {st.n} recorded days to {last_day}",
                           last_day.isoformat())  # fmt: skip
        expiry = await self._expiry(sym, params)
        if metric == "days_to_expiry":
            return Reading(Decimal((expiry - _today()).days), f"NSE contract info: expiry {expiry}")
        chain = await self._chain(sym, expiry)
        as_of = chain.as_of.isoformat() if chain.as_of else None
        if metric == "spot":
            if chain.underlying is None:
                return unknown(f"NSE's chain for {sym} has no underlying value", NSE_CHAIN)
            return Reading(D(chain.underlying), f"NSE option chain underlying ({expiry})", as_of)
        if metric == "strategy_pnl_pct_of_max_loss":
            return await self._strategy(sym, expiry, chain, params, as_of)
        return unknown(f"unknown F&O metric {metric}")

    async def _strategy(
        self, sym: str, expiry: date, chain, params: dict[str, str], as_of: str | None
    ) -> Reading:
        from finresearch.fincalc import options as o

        legs = parse_entry_legs(params.get("legs", ""))
        lot = await self._get(("lot", sym, expiry), lambda: (SOURCES.fno_lot or _live_lot)(sym, expiry))
        if not lot:
            return unknown(f"no NSE lot size for {sym} {expiry}")
        if chain.underlying is None:
            return unknown(f"NSE's chain for {sym} has no underlying value")
        by_strike = {float(r.strike): r for r in chain.rows}
        pnl = 0.0
        model = []
        for leg in legs:
            sign = 1 if leg["side"] == "buy" else -1
            qty = sign * leg["lots"] * lot
            if leg["right"] == "future":
                now = float(chain.underlying)  # the future's own price is not in the chain: spot is the proxy
            else:
                row = by_strike.get(leg["strike"])
                q = (row.call if leg["right"] == "call" else row.put) if row else None
                if q is None or not q.last_price:
                    return unknown(f"no last price for the {leg['strike']:g} {leg['right']} in NSE's chain")
                now = float(q.last_price)
            pnl += (now - leg["premium"]) * qty
            if leg["right"] == "future":  # fincalc's future leg takes its entry price as the strike
                model.append(o.Leg("future", leg["premium"], 0.0, qty))
            else:
                model.append(o.Leg(leg["right"], leg["strike"], leg["premium"], qty))
        prof = o.profile(model, float(chain.underlying))
        if prof.max_loss is None or prof.max_loss >= 0:
            return unknown(
                "the strategy's maximum loss is unlimited or zero; use a price or spot rule instead"
            )
        value = pnl / abs(prof.max_loss) * 100
        return Reading(D(round(value, 2)), f"mark-to-market P&L ₹{pnl:,.0f} vs maximum loss ₹{abs(prof.max_loss):,.0f} "
                       f"(NSE last prices, lot {lot}; costs not included)", as_of)  # fmt: skip

    # ------------------------------------------------------------------ portfolio
    async def _portfolio(
        self, metric: str, inst: str, params: dict[str, str], baseline: dict[str, Any]
    ) -> Reading:
        from finresearch.alerts.portfolio import portfolio_metrics

        if ("portfolio",) not in self.memo:
            self.memo[("portfolio",)] = portfolio_metrics(self.session)
        metrics, reason = self.memo[("portfolio",)]
        if reason:
            return unknown(reason, "finresearch.portfolio")
        if metric not in metrics:
            return unknown(f"the portfolio does not compute {metric} yet", "finresearch.portfolio")
        v, src, *rest = metrics[metric]  # (value, source) or (value, source, detail)
        detail = rest[0] if rest else None
        return Reading(D(v), src, detail=detail) if v is not None else unknown(src, "finresearch.portfolio")


_LEG = re.compile(r"^(buy|sell):(call|put|future):(\d+(?:\.\d+)?):(\d+):(\d+(?:\.\d+)?)$")


def parse_entry_legs(text: str) -> list[dict[str, Any]]:
    """Legs with the premium you entered at: "buy:call:22800:1:120.5,sell:call:23000:1:61" (side:right:strike:
    lots:entry premium; for a future, the premium is the entry price)."""
    out = []
    for part in [p.strip().lower() for p in (text or "").split(",") if p.strip()]:
        m = _LEG.match(part)
        if not m:
            raise ValueError(f"cannot read leg {part!r}; use side:right:strike:lots:entry premium")
        lots = int(m.group(4))
        if not 1 <= lots <= 100:
            raise ValueError(f"lots must be 1-100 in {part!r}")
        out.append({"side": m.group(1), "right": m.group(2), "strike": float(m.group(3)), "lots": lots,
                    "premium": float(m.group(5))})  # fmt: skip
    if not out or len(out) > 8:
        raise ValueError("give 1 to 8 legs")
    return out
