"""Stock-signal side routes: the forensic scorecard for one symbol and the momentum + trend backtest summary
(docs/dev/RESEARCH_ROADMAP.md §C.5, §D.2). The signal itself is served by /api/signals/stock/{symbol}."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from finresearch.api.markets import _symbol


def add_stock_signal_routes(app: FastAPI) -> None:
    @app.get("/api/stocks/{symbol}/forensic")
    async def stock_forensic(symbol: str) -> dict[str, Any]:
        """Piotroski, Altman Z''-EM, Beneish, accruals and CFO/EBITDA from the last two annual Integrated Filings.
        Screening flags, not buy/sell triggers."""
        from finresearch.signals import stock

        return stock.forensic(await stock.inputs(_symbol(symbol)))

    @app.get("/api/backtests/stock")
    def stock_backtest(months: bool = False) -> dict[str, Any]:
        """The committed backtest artefact (evals/stock_backtest/results.json); monthly rows only with ?months=1."""
        from finresearch.signals.stock import load_backtest

        bt = load_backtest()
        if bt is None:
            raise HTTPException(
                404, "no stock backtest has been run yet (python -m finresearch.evals.stock_backtest)"
            )
        out = {k: v for k, v in bt.items() if k != "months" or months}
        out["equity_curve"] = _curve(bt.get("months") or [])
        return out


def _curve(months: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Growth of 1 for the strategy, the equal-weight universe and the NIFTY 50 price index, month by month."""
    out, lv = [], {"strategy": 1.0, "equal_weight": 1.0, "nifty50": 1.0}
    for m in months:
        for k in lv:
            lv[k] *= 1 + m[k]
        out.append({"date": m["next"], **{k: round(v, 4) for k, v in lv.items()}})
    return out
