"""The F&O direction model's routes (#327): GET /api/fno/direction/registration, GET /api/fno/direction/{symbol}
(probabilities, model status, out-of-sample and live accuracy, the decision and its plan card) and POST
/api/fno/direction/{symbol}/log (log the latest session's forecasts to the ledger on demand).

Analysis only: nothing here places, modifies or cancels an order."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from fastapi import FastAPI, HTTPException

from finresearch.db import session_scope

# #354 h: the hash is over the registered spec's parameters, so "matches code" overstated it; matches_code stays one
# release as a deprecated alias of matches_spec
HASH_COVERS = (
    "spec parameters only (specs.registration()): the engine code that runs them is not hashed until an "
    "engine hash exists (roadmap P2.4)"
)


def registration() -> dict[str, Any]:
    from finresearch.direction import prereg, specs

    return {"version": specs.SPEC_VERSION, "hash": prereg.HASH, "registered_on": prereg.REGISTERED_ON,
            "matches_spec": specs.registration_hash() == prereg.HASH, "hash_covers": HASH_COVERS,
            "matches_code": specs.registration_hash() == prereg.HASH, "note": prereg.NOTE,
            "spec": specs.registration(), "doc": "evals/experiments/fno_direction/PREREG.md"}  # fmt: skip


def _probs_for(st: dict[str, Any], h: int) -> tuple[dict[str, float], bool, float | None]:
    """(probabilities the decision uses at horizon h, any accepted cell, IV30): accepted cells' logistic, else implied."""
    hz = next((x for x in st["horizons"] if x["h"] == h), None)
    if hz is None:
        raise HTTPException(422, f"horizon {h} is not one of the registered horizons")
    probs = {ev: v["shown"] for ev, v in hz["events"].items()}
    if any(p is None for p in probs.values()):
        raise HTTPException(422, f"no probability for every event at h={h} (no 30-day IV on {st['as_of']})")
    return probs, any(v["status"] == "accepted" for v in hz["events"].values()), st.get("iv_30")


def add_fno_direction_routes(
    app: FastAPI, fno: Callable[[], Any], clock: Callable[[], datetime] | None = None
) -> None:
    def now() -> datetime:
        return clock() if clock else datetime.now(UTC)

    def state(sym: str) -> dict[str, Any]:
        from sqlalchemy.exc import SQLAlchemyError

        from finresearch.direction import live

        try:
            return live.state(sym)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except SQLAlchemyError as e:
            raise HTTPException(503, f"F&O history unavailable: {type(e).__name__}") from e

    @app.get("/api/fno/direction/registration")
    def fno_direction_registration() -> dict[str, Any]:
        return registration()

    @app.get("/api/fno/direction/{symbol}")
    async def fno_direction(symbol: str, horizon: int | None = None, decide: bool = True) -> dict[str, Any]:
        """Today's probabilities and status, the stored out-of-sample evaluation, live accuracy from the ledger, and
        (with `decide`) the trade decision on the live chain with the chosen trade's plan card."""
        import asyncio

        from finresearch.direction import live, specs
        from finresearch.signals import fno as fs

        sym = symbol.strip().upper()
        st = await asyncio.to_thread(
            state, sym
        )  # a few seconds of history on a cold cache: off the event loop
        with session_scope() as s:
            accuracy = live.live_accuracy(s, sym)
        acc_h = [
            x["h"] for x in st["horizons"] if any(v["status"] == "accepted" for v in x["events"].values())
        ]
        h = horizon or (acc_h[0] if acc_h else specs.PRIMARY[1])
        out: dict[str, Any] = {**st, "live_accuracy": accuracy, "decision_horizon": h,
                               "registration": {k: v for k, v in registration().items() if k != "spec"},
                               "risk_notice": fs.risk_notice(), "evidence_note": EVIDENCE_NOTE}  # fmt: skip
        out["decision"] = await _decision(sym, st, h) if decide else None
        return out

    async def _decision(sym: str, st: dict[str, Any], h: int) -> dict[str, Any]:
        from finresearch.adapters.nse_fno import lot_size_for
        from finresearch.adapters.nse_holidays import trading_holidays
        from finresearch.api.fno_plan import plan_inputs
        from finresearch.direction import decide, live
        from finresearch.fincalc.dates import to_ist
        from finresearch.signals import fno as fs
        from finresearch.signals import tradeplan as tpl
        from finresearch.signals.rates import risk_free
        from finresearch.suggest.advisor import load_profile

        probs, accepted, iv30 = _probs_for(st, h)
        if not iv30:
            return {"status": "unavailable", "why": f"no 30-day IV stored for {st['as_of']}"}
        today = to_ist(now()).date()
        as_of = date.fromisoformat(st["as_of"])
        horizon_day = live.session_after(as_of, h, trading_holidays())
        try:
            async with fno() as f:
                exps = fs.live_expiries((await f.contract_info(sym))[0], today)
                expiry = next((e for e in exps if e >= horizon_day), None)
                if expiry is None:
                    return {
                        "status": "unavailable",
                        "why": f"no live expiry on or after {horizon_day:%d-%b-%Y}",
                    }
                chain = await f.option_chain(sym, expiry)
                lot = lot_size_for(await f.lot_sizes(), sym, expiry)
                if chain.underlying is None or not chain.rows or not lot:
                    return {"status": "unavailable", "why": f"no option chain or lot size for {sym} {expiry}"}
                rate = (await risk_free(max((expiry - today).days, 0.5) / 365))["rate"]
                with session_scope() as s:
                    brokerage = float(load_profile(s).fno_brokerage_per_order_inr)
                d = decide.decide(chain, lot, today=today, horizon_day=horizon_day, h=h, iv_30=iv30,
                                  probs_model=probs, accepted=accepted, rate=rate, brokerage=brokerage)  # fmt: skip
                d["status"] = "ok"
                d["chain"] = {
                    "source": chain.source,
                    "as_of": chain.as_of.isoformat() if chain.as_of else None,
                }
                d["probabilities_as_of"] = st["as_of"]
                if d["chosen"] is not None:
                    legs = [fs.LegIn(x["right"], x["strike"], x["side"]) for x in d["chosen"]["legs"]]
                    x = await plan_inputs(f, sym, expiry, today)
                    try:
                        d["plan"] = tpl.build(chain, lot, legs, x, title=f"{sym} {expiry:%d-%b-%Y} · "
                                              f"{d['chosen']['label']} (direction model)")  # fmt: skip
                    except ValueError as e:
                        d["plan"] = None
                        d["notes"].append(f"plan card unavailable: {e}")
                else:
                    d["plan"] = None
                    d["non_directional"] = {"text": "No measured directional edge. Non-directional plans: the "
                                            "pre-registered library (iron condors, credit spreads) on today's chain, "
                                            "each with its own out-of-sample backtest.",
                                            "suggest_api": f"/api/fno/{sym}/suggest", "href": f"/fno?symbol={sym}",
                                            "evidence_href": "/fno/backtests"}  # fmt: skip
                return d
        except HTTPException:
            raise
        except Exception as e:  # the chain source refusing must not hide the probabilities and accuracy
            return {"status": "unavailable", "why": f"live chain unavailable: {type(e).__name__}: {e}"[:300]}

    @app.post("/api/fno/direction/{symbol}/log")
    def fno_direction_log(symbol: str) -> dict[str, Any]:
        """Log the latest stored session's forecasts now (allowed from that session's close to the next open)."""
        from finresearch.direction import live

        sym = symbol.strip().upper()
        st = state(sym)
        return live.log(sym, now(), st=st)


EVIDENCE_NOTE = ("Probabilities are for the index's close h sessions after the stored close they were computed from. "
                 "'Market-implied, no edge' means the option market's own probability (30-day IV, lognormal): a "
                 "trade priced at fair value has an expected loss equal to its costs. A model is shown only when it "
                 "passed every pre-registered out-of-sample criterion. Not advice; nothing is ordered.")  # fmt: skip
